"""Sub-agent event contracts and the event-log projection of a sub-agent tree.

The sub-agent hierarchy is never a separate mutable resource: it is projected
from the execution's ordered event log.  A ``sub_agent.spawned`` event creates a
node, later ``sub_agent.*`` events advance its status and input acceptance, and
``workflow.gate.*`` events mark human-input gates on the owning orchestrator.
Because every event keeps its single global execution cursor, a projection can
filter by node (or the whole subtree) without breaking resume or event-id
deduplication.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from thesis.execution.events import (
    ActorScope,
    EventReplay,
    ExecutionEvent,
    ExecutionEventStore,
    ExecutionEventType,
)

SUB_AGENT_EVENT_TYPES: frozenset[ExecutionEventType] = frozenset(
    {
        ExecutionEventType.SUB_AGENT_SPAWNED,
        ExecutionEventType.SUB_AGENT_PROGRESS,
        ExecutionEventType.SUB_AGENT_OUTPUT,
        ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
        ExecutionEventType.SUB_AGENT_COMPLETED,
        ExecutionEventType.SUB_AGENT_FAILED,
    }
)

GATE_EVENT_TYPES: frozenset[ExecutionEventType] = frozenset(
    {
        ExecutionEventType.WORKFLOW_GATE_BLOCKED,
        ExecutionEventType.WORKFLOW_GATE_RESOLVED,
    }
)

#: Status values projected for one sub-agent node.
STATUS_RUNNING = "running"
STATUS_NEEDS_INPUT = "needs_input"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

_STATUS_BY_EVENT: Mapping[ExecutionEventType, str] = {
    ExecutionEventType.SUB_AGENT_SPAWNED: STATUS_RUNNING,
    ExecutionEventType.SUB_AGENT_PROGRESS: STATUS_RUNNING,
    ExecutionEventType.SUB_AGENT_OUTPUT: STATUS_RUNNING,
    ExecutionEventType.SUB_AGENT_NEEDS_INPUT: STATUS_NEEDS_INPUT,
    ExecutionEventType.SUB_AGENT_COMPLETED: STATUS_COMPLETED,
    ExecutionEventType.SUB_AGENT_FAILED: STATUS_FAILED,
}


class SubAgentContractError(ValueError):
    """Raised when a sub-agent event payload violates the contract."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SubAgentContextSpec:
    """The isolated execution context advertised for one sub-agent node."""

    knowledge_cutoff: datetime | None = None
    mcp_enabled: bool = False
    mcp_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.knowledge_cutoff is not None:
            if self.knowledge_cutoff.tzinfo is None or (
                self.knowledge_cutoff.utcoffset() is None
            ):
                raise SubAgentContractError("knowledge_cutoff must be timezone-aware")
            object.__setattr__(
                self, "knowledge_cutoff", self.knowledge_cutoff.astimezone(UTC)
            )
        tools = tuple(
            dict.fromkeys(
                tool for tool in self.mcp_tools if isinstance(tool, str) and tool
            )
        )
        object.__setattr__(self, "mcp_tools", tools)

    def as_dict(self) -> dict[str, object]:
        """Return the transport form of this context with ``isolated`` truthy."""
        return {
            "isolated": True,
            "knowledge_cutoff": (
                None if self.knowledge_cutoff is None else _iso(self.knowledge_cutoff)
            ),
            "mcp": {"enabled": self.mcp_enabled, "tools": list(self.mcp_tools)},
        }


def sub_agent_payload(
    *,
    sub_agent_id: str,
    parent_sub_agent_id: str | None,
    phase: str,
    role: str,
    context: SubAgentContextSpec,
    accepts_input: bool = False,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the canonical payload shared by every ``sub_agent.*`` event."""
    if not isinstance(sub_agent_id, str) or not sub_agent_id.strip():
        raise SubAgentContractError("subAgentId must be non-empty")
    if parent_sub_agent_id is not None and (
        not isinstance(parent_sub_agent_id, str) or not parent_sub_agent_id.strip()
    ):
        raise SubAgentContractError("parentSubAgentId must be null or non-empty")
    payload: dict[str, object] = {
        "acceptsInput": bool(accepts_input),
        "context": context.as_dict(),
        "parentSubAgentId": parent_sub_agent_id,
        "phase": phase,
        "role": role,
        "subAgentId": sub_agent_id,
    }
    if extra is not None:
        payload.update(extra)
    return payload


def append_sub_agent_event(
    store: ExecutionEventStore,
    *,
    execution_id: str,
    workspace_id: str,
    event_type: ExecutionEventType,
    occurred_at: datetime,
    sub_agent_id: str,
    parent_sub_agent_id: str | None,
    phase: str,
    role: str,
    context: SubAgentContextSpec,
    accepts_input: bool = False,
    actor_id: str = "thesis-workflow",
    actor_scope: ActorScope = ActorScope.WORKFLOW,
    correlation_id: str | None = None,
    intent_epoch: int = 0,
    extra: Mapping[str, object] | None = None,
) -> ExecutionEvent:
    """Append one sub-agent event carrying the canonical node payload."""
    payload = sub_agent_payload(
        sub_agent_id=sub_agent_id,
        parent_sub_agent_id=parent_sub_agent_id,
        phase=phase,
        role=role,
        context=context,
        accepts_input=accepts_input,
        extra=extra,
    )
    return store.append(
        ExecutionEvent(
            execution_id=execution_id,
            workspace_id=workspace_id,
            event_type=event_type,
            correlation_id=correlation_id or execution_id,
            actor_id=actor_id,
            actor_scope=actor_scope,
            occurred_at=occurred_at,
            payload=payload,
            intent_epoch=intent_epoch,
        )
    )


def append_gate_event(
    store: ExecutionEventStore,
    *,
    execution_id: str,
    workspace_id: str,
    event_type: ExecutionEventType,
    occurred_at: datetime,
    from_phase: str,
    to_phase: str,
    gate: str,
    reason: str,
    sub_agent_id: str | None = None,
    accepts_input: bool = False,
    actor_id: str = "thesis-workflow",
    actor_scope: ActorScope = ActorScope.WORKFLOW,
    correlation_id: str | None = None,
    intent_epoch: int = 0,
) -> ExecutionEvent:
    """Append a gate blocked/resolved event bound to an owning orchestrator."""
    if event_type not in GATE_EVENT_TYPES:
        raise SubAgentContractError("event_type must be a workflow.gate.* event")
    return store.append(
        ExecutionEvent(
            execution_id=execution_id,
            workspace_id=workspace_id,
            event_type=event_type,
            correlation_id=correlation_id or execution_id,
            actor_id=actor_id,
            actor_scope=actor_scope,
            occurred_at=occurred_at,
            payload={
                "acceptsInput": bool(accepts_input),
                "fromPhase": from_phase,
                "gate": gate,
                "reason": reason,
                "subAgentId": sub_agent_id,
                "toPhase": to_phase,
            },
            intent_epoch=intent_epoch,
        )
    )


@dataclass(frozen=True, slots=True)
class SubAgentNode:
    """One projected sub-agent record including its direct children."""

    sub_agent_id: str
    parent_sub_agent_id: str | None
    phase: str
    role: str
    status: str
    accepts_input: bool
    context: Mapping[str, object]
    cursor: int
    children: tuple[SubAgentNode, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return the snake_case REST record for this node and its subtree."""
        return {
            "accepts_input": self.accepts_input,
            "children": [child.as_dict() for child in self.children],
            "context": dict(self.context),
            "parent_sub_agent_id": self.parent_sub_agent_id,
            "phase": self.phase,
            "role": self.role,
            "status": self.status,
            "sub_agent_id": self.sub_agent_id,
        }


@dataclass(frozen=True, slots=True)
class SubAgentTree:
    """The immutable projection of an execution's sub-agent hierarchy."""

    execution_id: str
    roots: tuple[SubAgentNode, ...]
    nodes: Mapping[str, SubAgentNode]

    @classmethod
    def from_events(
        cls, execution_id: str, events: Iterable[ExecutionEvent]
    ) -> SubAgentTree:
        """Project the tree from an ordered execution event stream."""
        spawns: dict[str, dict[str, object]] = {}
        spawn_cursors: dict[str, int] = {}
        status: dict[str, str] = {}
        accepts: dict[str, bool] = {}
        order: list[str] = []
        for event in events:
            event_type = event.event_type
            payload = event.payload
            sub_agent_id = payload.get("subAgentId")
            if not isinstance(sub_agent_id, str) or not sub_agent_id:
                continue
            if event_type is ExecutionEventType.SUB_AGENT_SPAWNED:
                if sub_agent_id not in spawns:
                    order.append(sub_agent_id)
                spawns[sub_agent_id] = dict(payload)
                spawn_cursors[sub_agent_id] = event.cursor
                status[sub_agent_id] = STATUS_RUNNING
                accepts[sub_agent_id] = bool(payload.get("acceptsInput"))
            elif sub_agent_id in spawns:
                projected = _STATUS_BY_EVENT.get(event_type)
                if projected is not None:
                    status[sub_agent_id] = projected
                if "acceptsInput" in payload:
                    accepts[sub_agent_id] = bool(payload.get("acceptsInput"))
                if event_type in {
                    ExecutionEventType.SUB_AGENT_COMPLETED,
                    ExecutionEventType.SUB_AGENT_FAILED,
                }:
                    accepts[sub_agent_id] = False
            if (
                event_type is ExecutionEventType.WORKFLOW_GATE_RESOLVED
                and sub_agent_id in spawns
            ):
                accepts[sub_agent_id] = False
            if (
                event_type is ExecutionEventType.SUB_AGENT_NEEDS_INPUT
                and sub_agent_id in spawns
            ):
                accepts[sub_agent_id] = True

        children: dict[str | None, list[str]] = {}
        for sub_agent_id in order:
            parent = spawns[sub_agent_id].get("parentSubAgentId")
            if not isinstance(parent, str) or parent not in spawns:
                parent = None
            children.setdefault(parent, []).append(sub_agent_id)

        by_id: dict[str, SubAgentNode] = {}

        def build(sub_agent_id: str) -> SubAgentNode:
            spawn = spawns[sub_agent_id]
            context = spawn.get("context")
            context_mapping: Mapping[str, object] = (
                dict(context) if isinstance(context, Mapping) else {}
            )
            parent_value = spawn.get("parentSubAgentId")
            resolved_parent = parent_value if isinstance(parent_value, str) else None
            node = SubAgentNode(
                sub_agent_id=sub_agent_id,
                parent_sub_agent_id=resolved_parent,
                phase=str(spawn.get("phase", "")),
                role=str(spawn.get("role", "")),
                status=status[sub_agent_id],
                accepts_input=accepts[sub_agent_id],
                context=context_mapping,
                cursor=spawn_cursors.get(sub_agent_id, 0),
                children=tuple(
                    build(child) for child in children.get(sub_agent_id, [])
                ),
            )
            by_id[sub_agent_id] = node
            return node

        roots = tuple(build(root) for root in children.get(None, []))
        return cls(execution_id=execution_id, roots=roots, nodes=by_id)

    def get(self, sub_agent_id: str) -> SubAgentNode | None:
        """Return one node by id, or ``None`` when it was never spawned."""
        return self.nodes.get(sub_agent_id)

    def children_of(self, parent_sub_agent_id: str | None) -> tuple[SubAgentNode, ...]:
        """Return direct children of a parent (``None`` selects top-level)."""
        if parent_sub_agent_id is None:
            return self.roots
        node = self.nodes.get(parent_sub_agent_id)
        return () if node is None else node.children

    def subtree_ids(self, sub_agent_id: str) -> frozenset[str]:
        """Return the node id plus every descendant id in its subtree."""
        node = self.nodes.get(sub_agent_id)
        if node is None:
            return frozenset()
        collected: set[str] = set()

        def walk(current: SubAgentNode) -> None:
            collected.add(current.sub_agent_id)
            for child in current.children:
                walk(child)

        walk(node)
        return frozenset(collected)

    def own_ids(self, sub_agent_id: str) -> frozenset[str]:
        """Return just the node id, ignoring descendants."""
        return frozenset({sub_agent_id}) if sub_agent_id in self.nodes else frozenset()


def _event_sub_agent_id(event: ExecutionEvent) -> str | None:
    value = event.payload.get("subAgentId")
    return value if isinstance(value, str) and value else None


def filter_replay(
    replay: EventReplay,
    node_ids: frozenset[str],
    *,
    limit: int | None = None,
) -> EventReplay:
    """Filter a global replay page to selected nodes while keeping its cursors.

    ``after_cursor`` and every retained event keep the execution's single
    global cursor, so a filtered page resumes exactly where the unfiltered
    stream would and event-id deduplication still holds.
    """
    selected = tuple(
        event for event in replay.events if _event_sub_agent_id(event) in node_ids
    )
    has_more = limit is not None and len(selected) > limit
    if limit is not None:
        selected = selected[:limit]
    next_cursor = replay.after_cursor if not selected else selected[-1].cursor
    return EventReplay(
        execution_id=replay.execution_id,
        after_cursor=replay.after_cursor,
        events=selected,
        next_cursor=next_cursor,
        has_more=has_more,
    )


__all__ = [
    "GATE_EVENT_TYPES",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_NEEDS_INPUT",
    "STATUS_RUNNING",
    "SUB_AGENT_EVENT_TYPES",
    "SubAgentContextSpec",
    "SubAgentContractError",
    "SubAgentNode",
    "SubAgentTree",
    "append_gate_event",
    "append_sub_agent_event",
    "filter_replay",
    "sub_agent_payload",
]
