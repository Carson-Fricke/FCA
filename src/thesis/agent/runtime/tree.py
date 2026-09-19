"""Chat-scoped projection of the durable sub-agent hierarchy.

Unlike the execution projection, which replays a workflow execution's event log,
this projection is built from the first-class :class:`SubAgentRecord` store.  A
sub-agent is attached to a chat, not to an execution, so a chat-scoped tree is
the natural ownership view: it survives a process restart, and it includes
children spawned directly from chat that have no workflow execution at all.

The projection is a pure function of the durable records, so the same tree is
reconstructed after a restart without inventing or dropping nodes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from thesis.chat.contracts import SubAgentRecord


class ChatSubAgentProjectionError(ValueError):
    """Raised when durable sub-agent records cannot form a tree."""


@dataclass(frozen=True, slots=True)
class ChatSubAgentNode:
    """One chat-scoped sub-agent node and its direct children."""

    sub_agent_id: str
    chat_id: str
    role: str
    kind: str
    status: str
    parent_sub_agent_id: str | None
    workflow_execution_id: str | None
    phase: str | None
    accepts_input: bool
    allowed_tools: tuple[str, ...]
    delegated_budget: int
    input_contract_ref: str | None
    completion_contract_ref: str | None
    result_ref: str | None
    result: Mapping[str, object] | None
    created_at: datetime
    updated_at: datetime | None
    children: tuple[ChatSubAgentNode, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready node including its subtree."""
        return {
            "subAgentId": self.sub_agent_id,
            "chatId": self.chat_id,
            "role": self.role,
            "kind": self.kind,
            "status": self.status,
            "parentSubAgentId": self.parent_sub_agent_id,
            "workflowExecutionId": self.workflow_execution_id,
            "phase": self.phase,
            "acceptsInput": self.accepts_input,
            "allowedTools": list(self.allowed_tools),
            "delegatedBudget": self.delegated_budget,
            "inputContractRef": self.input_contract_ref,
            "completionContractRef": self.completion_contract_ref,
            "resultRef": self.result_ref,
            "result": None if self.result is None else dict(self.result),
            "createdAt": self.created_at.isoformat(),
            "updatedAt": None
            if self.updated_at is None
            else self.updated_at.isoformat(),
            "children": [child.as_dict() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class ChatSubAgentTree:
    """The chat-scoped roots plus a flat index for direct lookup."""

    chat_id: str
    roots: tuple[ChatSubAgentNode, ...]
    nodes: Mapping[str, ChatSubAgentNode]

    def get(self, sub_agent_id: str) -> ChatSubAgentNode | None:
        """Return one node by id, or ``None`` when it is not in this chat."""
        return self.nodes.get(sub_agent_id)

    def ids(self) -> frozenset[str]:
        """Return every node id in the tree."""
        return frozenset(self.nodes)

    def subtree_ids(self, sub_agent_id: str) -> frozenset[str]:
        """Return one node's id plus every descendant id."""
        if sub_agent_id not in self.nodes:
            return frozenset()
        by_parent: dict[str | None, list[str]] = {}
        for node in self.nodes.values():
            by_parent.setdefault(node.parent_sub_agent_id, []).append(node.sub_agent_id)
        collected: set[str] = set()
        frontier = [sub_agent_id]
        while frontier:
            current = frontier.pop()
            if current in collected:
                continue
            collected.add(current)
            frontier.extend(by_parent.get(current, ()))
        return frozenset(collected)

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready tree."""
        return {
            "chatId": self.chat_id,
            "roots": [root.as_dict() for root in self.roots],
            "items": [node.as_dict() for node in self.nodes.values()],
        }


def project_chat_sub_agents(
    records: Iterable[SubAgentRecord],
    *,
    workspace_id: str,
    chat_id: str,
) -> ChatSubAgentTree:
    """Project durable records for one chat into a nested tree."""
    scoped = [
        record
        for record in records
        if record.workspace_id == workspace_id and record.chat_id == chat_id
    ]
    known = {record.sub_agent_id for record in scoped}
    children: dict[str | None, list[SubAgentRecord]] = {}
    for record in scoped:
        parent = (
            record.parent_sub_agent_id if record.parent_sub_agent_id in known else None
        )
        children.setdefault(parent, []).append(record)
    for bucket in children.values():
        bucket.sort(key=lambda record: (record.created_at, record.sub_agent_id))

    built: dict[str, ChatSubAgentNode] = {}
    visiting: set[str] = set()

    def build(record: SubAgentRecord) -> ChatSubAgentNode:
        if record.sub_agent_id in visiting:
            raise ChatSubAgentProjectionError("sub-agent parent cycle detected")
        visiting.add(record.sub_agent_id)
        nested = tuple(build(child) for child in children.get(record.sub_agent_id, ()))
        visiting.discard(record.sub_agent_id)
        node = ChatSubAgentNode(
            sub_agent_id=record.sub_agent_id,
            chat_id=record.chat_id,
            role=record.role,
            kind=record.kind.value,
            status=record.status.value,
            parent_sub_agent_id=record.parent_sub_agent_id,
            workflow_execution_id=record.workflow_execution_id,
            phase=record.phase,
            accepts_input=record.accepts_input,
            allowed_tools=tuple(record.allowed_tools),
            delegated_budget=record.delegated_budget,
            input_contract_ref=record.input_contract_ref,
            completion_contract_ref=record.completion_contract_ref,
            result_ref=record.result_ref,
            result=record.result,
            created_at=record.created_at,
            updated_at=record.updated_at,
            children=nested,
        )
        built[record.sub_agent_id] = node
        return node

    roots = tuple(build(record) for record in children.get(None, ()))
    return ChatSubAgentTree(chat_id=chat_id, roots=roots, nodes=built)


__all__ = [
    "ChatSubAgentNode",
    "ChatSubAgentProjectionError",
    "ChatSubAgentTree",
    "project_chat_sub_agents",
]
