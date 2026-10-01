"""Durable sub-agent lifecycle: spawn, input, complete, fail, and cancel.

The lifecycle owns the deterministic guards the specification requires: chat and
workspace scope inheritance, maximum depth, maximum active children, delegated
budget inheritance, and the one-shot input rule.  Records persist through the
chat persistence port, so a sub-agent spawned from a chat or a workflow phase is
equally durable and recoverable.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType

from thesis.agent.runtime.specs import (
    DEFAULT_MAX_CHILDREN,
    DEFAULT_MAX_DEPTH,
    SubAgentSpec,
)
from thesis.agent.runtime.store import PersistenceSubAgentStore, SubAgentStore
from thesis.chat.contracts import (
    WORKFLOW_NODE_ROLE,
    ChatContractError,
    RecordNotFound,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
    WorkflowExecutionState,
    replace_record,
)
from thesis.chat.service import ChatService
from thesis.execution.events import ExecutionEventType
from thesis.execution.sub_agents import append_sub_agent_event

_TERMINAL_STATUSES = frozenset(
    {
        SubAgentStatus.COMPLETED,
        SubAgentStatus.FAILED,
        SubAgentStatus.CANCELLED,
    }
)

#: Statuses that occupy a concurrency slot.  ``NEEDS_INPUT`` is deliberately
#: absent: a node paused for operator review is not doing work.
_IN_FLIGHT_STATUSES = frozenset(
    {
        SubAgentStatus.PENDING,
        SubAgentStatus.RUNNING,
    }
)


class SubAgentLifecycleError(RuntimeError):
    """Raised when a sub-agent lifecycle command violates its contract."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _subtree_order(
    records: Iterable[SubAgentRecord], root_ids: Iterable[str]
) -> list[str]:
    """Return the given roots and their descendants in deterministic tree order.

    Unknown roots are skipped and a parent cycle cannot loop forever, so the
    sweep is total even over damaged records.
    """
    children: dict[str | None, list[str]] = {}
    known: set[str] = set()
    for record in records:
        known.add(record.sub_agent_id)
        children.setdefault(record.parent_sub_agent_id, []).append(record.sub_agent_id)
    ordered: list[str] = []
    seen: set[str] = set()
    stack = [root for root in root_ids if root in known]
    stack.reverse()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        stack.extend(reversed(children.get(current, ())))
    return ordered


class SubAgentLifecycle:
    """Deterministic lifecycle commands over durable sub-agent records."""

    def __init__(
        self,
        chat: ChatService,
        *,
        store: SubAgentStore,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_children: int = DEFAULT_MAX_CHILDREN,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
    ) -> None:
        if max_depth < 1:
            raise SubAgentLifecycleError("max_depth must be at least one")
        if max_children < 1:
            raise SubAgentLifecycleError("max_children must be at least one")
        self._chat = chat
        self._store = store
        self._max_depth = max_depth
        self._max_children = max_children
        self._clock = clock
        self._id_factory = id_factory

    def get(self, workspace_id: str, sub_agent_id: str) -> SubAgentRecord | None:
        return self._store.get_sub_agent(workspace_id, sub_agent_id)

    def children(
        self, workspace_id: str, parent_sub_agent_id: str | None
    ) -> tuple[SubAgentRecord, ...]:
        return tuple(
            record
            for record in self._store.list_sub_agents(workspace_id)
            if record.parent_sub_agent_id == parent_sub_agent_id
        )

    def depth(self, workspace_id: str, sub_agent_id: str) -> int:
        """Return the ancestor depth of a node (a root has depth zero)."""
        depth = 0
        current: SubAgentRecord | None = self.get(workspace_id, sub_agent_id)
        seen: set[str] = set()
        while current is not None and current.parent_sub_agent_id is not None:
            if current.sub_agent_id in seen:
                raise SubAgentLifecycleError("sub-agent parent cycle detected")
            seen.add(current.sub_agent_id)
            depth += 1
            current = self.get(workspace_id, current.parent_sub_agent_id)
        return depth

    def workflow_node(
        self, workspace_id: str, execution_id: str
    ) -> SubAgentRecord | None:
        """Return the durable workflow node owning one execution's phase agents."""
        for record in self._store.list_sub_agents(workspace_id):
            if (
                record.workflow_execution_id == execution_id
                and record.role == WORKFLOW_NODE_ROLE
            ):
                return record
        return None

    def spawn(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        spec: SubAgentSpec,
        actor_id: str,
        parent_sub_agent_id: str | None = None,
        spawned_by_invocation_id: str | None = None,
        workflow_execution_id: str | None = None,
        phase_attempt_id: str | None = None,
        sub_agent_id: str | None = None,
    ) -> SubAgentRecord:
        """Create one durable child after enforcing every deterministic guard.

        The active-children cap counts only in-flight statuses (``pending`` and
        ``running``).  A node paused for operator review (``needs_input``) is
        not doing work, so it does not hold a slot; the trade-off is that a
        chat may hold several paused nodes at the same level, but the
        nesting-depth guard still bounds the tree and each paused node must be
        resumed or cancelled before its conversation can grow any further.
        """
        parent = (
            None
            if parent_sub_agent_id is None
            else self.get(workspace_id, parent_sub_agent_id)
        )
        if parent_sub_agent_id is not None and parent is None:
            raise SubAgentLifecycleError("parent sub-agent does not exist")
        if parent is not None and parent.chat_id != chat_id:
            raise SubAgentLifecycleError("parent sub-agent belongs to another chat")
        if parent is not None:
            depth = self.depth(workspace_id, parent.sub_agent_id) + 1
            if depth > self._max_depth:
                raise SubAgentLifecycleError(
                    f"sub-agent depth exceeds the maximum of {self._max_depth}"
                )
            if spec.delegated_budget > parent.delegated_budget:
                raise SubAgentLifecycleError(
                    "child delegated budget exceeds the parent's delegated budget"
                )
        # The concurrency guard is per conversation: a chat owns its own budget of
        # in-flight agents, and work stranded in another chat can never block this
        # one.  Only pending/running children hold a slot; a child paused for
        # operator review does not, so pausing never blocks new work.
        siblings = (
            record
            for record in self.children(workspace_id, parent_sub_agent_id)
            if record.chat_id == chat_id
        )
        in_flight = [
            record for record in siblings if record.status in _IN_FLIGHT_STATUSES
        ]
        if len(in_flight) >= self._max_children:
            raise SubAgentLifecycleError(
                f"maximum of {self._max_children} active children reached"
            )
        now = self._clock()
        record = SubAgentRecord(
            workspace_id=workspace_id,
            created_at=now,
            created_by=actor_id,
            sub_agent_id=sub_agent_id or self._id_factory("subagent"),
            chat_id=chat_id,
            role=spec.role,
            parent_sub_agent_id=parent_sub_agent_id,
            spawned_by_invocation_id=spawned_by_invocation_id,
            workflow_execution_id=workflow_execution_id,
            phase_attempt_id=phase_attempt_id,
            phase=spec.phase,
            spec_version=spec.spec_version,
            kind=spec.kind,
            allowed_tools=spec.allowed_tools,
            status=SubAgentStatus.PENDING,
            accepts_input=spec.kind is SubAgentKind.CONVERSATION,
            input_contract_ref=spec.input_contract_ref,
            completion_contract_ref=spec.completion_contract_ref,
            delegated_budget=spec.delegated_budget,
            updated_at=now,
        )
        return self._store.save_sub_agent(record)

    def start(self, workspace_id: str, sub_agent_id: str) -> SubAgentRecord:
        record = self.get(workspace_id, sub_agent_id)
        accepts_input = record is not None and record.kind is SubAgentKind.CONVERSATION
        return self._update(
            workspace_id,
            sub_agent_id,
            status=SubAgentStatus.RUNNING,
            accepts_input=accepts_input,
        )

    def request_input(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        accepts_input: bool = True,
    ) -> SubAgentRecord:
        """Move a node to ``needs_input`` so an operator reply may be accepted.

        The flip is projected onto the owning execution's event log as a
        ``sub_agent.needs_input`` event, so live surfaces see the pause
        without re-reading the record store.  A node without a workflow
        execution has no event log; its state travels through the record
        store alone.
        """
        record = self._update(
            workspace_id,
            sub_agent_id,
            status=SubAgentStatus.NEEDS_INPUT,
            accepts_input=accepts_input,
        )
        if record.workflow_execution_id is not None:
            append_sub_agent_event(
                self._chat.event_store,
                execution_id=record.workflow_execution_id,
                workspace_id=record.workspace_id,
                event_type=ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
                occurred_at=record.updated_at or datetime.now(UTC),
                sub_agent_id=record.sub_agent_id,
                parent_sub_agent_id=record.parent_sub_agent_id,
                phase=record.phase or "",
                role=record.role,
                accepts_input=True,
            )
        return record

    def resume(self, workspace_id: str, sub_agent_id: str) -> SubAgentRecord:
        """Consume operator input and return the node to ``running``."""
        existing = self.get(workspace_id, sub_agent_id)
        return self._update(
            workspace_id,
            sub_agent_id,
            status=SubAgentStatus.RUNNING,
            accepts_input=(
                existing is not None and existing.kind is SubAgentKind.CONVERSATION
            ),
        )

    def complete(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        result: Mapping[str, object] | None = None,
        result_ref: str | None = None,
    ) -> SubAgentRecord:
        return self._update(
            workspace_id,
            sub_agent_id,
            status=SubAgentStatus.COMPLETED,
            accepts_input=False,
            result=result,
            result_ref=result_ref,
        )

    def fail(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        result: Mapping[str, object] | None = None,
    ) -> SubAgentRecord:
        return self._update(
            workspace_id,
            sub_agent_id,
            status=SubAgentStatus.FAILED,
            accepts_input=False,
            result=result,
        )

    def cancel(self, workspace_id: str, sub_agent_id: str) -> SubAgentRecord:
        return self._update(
            workspace_id,
            sub_agent_id,
            status=SubAgentStatus.CANCELLED,
            accepts_input=False,
        )

    def cancel_subtree(self, workspace_id: str, sub_agent_id: str) -> tuple[str, ...]:
        """Cancel one node and every non-terminal descendant of it.

        The affordance is idempotent and history-preserving: terminal records
        (for example an already-completed child) are left exactly as they are,
        and only records that actually transitioned to ``cancelled`` are
        returned, in root-first tree order.
        """
        records = self._store.list_sub_agents(workspace_id)
        if self.get(workspace_id, sub_agent_id) is None:
            raise SubAgentLifecycleError("sub-agent does not exist")
        cancelled: list[str] = []
        for node_id in _subtree_order(records, (sub_agent_id,)):
            record = self.get(workspace_id, node_id)
            if record is None or record.status in _TERMINAL_STATUSES:
                continue
            self.cancel(workspace_id, node_id)
            cancelled.append(node_id)
        return tuple(cancelled)

    def reconcile_stranded(self, workspace_id: str) -> tuple[str, ...]:
        """Cancel durable records stranded by a process restart.

        This is a platform-owned reconciliation, not an agent command: it
        writes the terminal ``cancelled`` status directly through the store
        instead of routing every record through :meth:`cancel` and its guards.
        Startup recovery must be total and idempotent — it must not fail on a
        damaged record, it must be safe to rerun after a crash mid-sweep, and
        it must never rewrite a record that already reached a terminal status.

        A non-terminal record is stranded when either:

        * it is a workflow node whose execution is not running (the execution
          was just stopped at startup, already reached a terminal state, or no
          longer exists), or
        * its chat no longer resolves to a chat in the workspace.

        Descendants of a stranded node are cancelled with it.  A running
        execution's records are left untouched.
        """
        records = self._store.list_sub_agents(workspace_id)
        roots: list[str] = []
        for record in records:
            if record.status in _TERMINAL_STATUSES:
                continue
            stranded = not self._chat_exists(workspace_id, record.chat_id)
            if (
                not stranded
                and record.role == WORKFLOW_NODE_ROLE
                and record.workflow_execution_id is not None
            ):
                stranded = not self._execution_running(
                    workspace_id, record.workflow_execution_id
                )
            if stranded:
                roots.append(record.sub_agent_id)
        now = self._clock()
        by_id = {record.sub_agent_id: record for record in records}
        cancelled: list[str] = []
        for node_id in _subtree_order(records, roots):
            record = by_id[node_id]
            if record.status in _TERMINAL_STATUSES:
                continue
            updated = replace_record(
                record,
                status=SubAgentStatus.CANCELLED,
                accepts_input=False,
                updated_at=now,
            )
            self._store.save_sub_agent(updated)
            cancelled.append(node_id)
        return tuple(cancelled)

    def _chat_exists(self, workspace_id: str, chat_id: str) -> bool:
        try:
            self._chat.get_chat(workspace_id, chat_id)
        except RecordNotFound:
            return False
        return True

    def _execution_running(self, workspace_id: str, execution_id: str) -> bool:
        try:
            execution = self._chat.get_execution(workspace_id, execution_id)
        except RecordNotFound:
            return False
        return execution.state is WorkflowExecutionState.RUNNING

    def accepts_unsolicited_input(self, record: SubAgentRecord) -> bool:
        """Return whether an operator may send a message without being asked.

        A conversation sub-agent always accepts unsolicited input while it is
        active; a one-shot sub-agent accepts input only after requesting it.
        """
        if record.status in _TERMINAL_STATUSES:
            return False
        if record.kind is SubAgentKind.CONVERSATION:
            return True
        return record.status is SubAgentStatus.NEEDS_INPUT and record.accepts_input

    def _update(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        status: SubAgentStatus,
        accepts_input: bool,
        result: Mapping[str, object] | None = None,
        result_ref: str | None = None,
    ) -> SubAgentRecord:
        record = self.get(workspace_id, sub_agent_id)
        if record is None:
            raise SubAgentLifecycleError("sub-agent does not exist")
        if record.status in _TERMINAL_STATUSES and status not in _TERMINAL_STATUSES:
            raise SubAgentLifecycleError(
                f"cannot move a terminal sub-agent to {status.value}"
            )
        if (
            status is SubAgentStatus.NEEDS_INPUT
            and record.kind is SubAgentKind.ONE_SHOT
        ):
            accepts_input = True
        changes: dict[str, object] = {
            "status": status,
            "accepts_input": accepts_input,
            "updated_at": self._clock(),
        }
        if result is not None:
            changes["result"] = MappingProxyType(dict(result))
        if result_ref is not None:
            changes["result_ref"] = result_ref
        try:
            updated = replace_record(record, **changes)
        except ChatContractError as error:
            raise SubAgentLifecycleError(str(error)) from error
        return self._store.save_sub_agent(updated)


def reconcile_stranded_sub_agents(
    chat: ChatService,
    workspace_id: str,
    *,
    store: SubAgentStore | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> tuple[str, ...]:
    """Cancel durable sub-agents stranded by a restart, for one workspace.

    Platform-owned entry point for :meth:`SubAgentLifecycle.reconcile_stranded`.
    Compose it at startup next to ``ChatService.stop_active_executions`` so a
    restarted process leaves no non-terminal record behind for work it cannot
    run.  Returns the ids that transitioned to ``cancelled``.
    """
    lifecycle = SubAgentLifecycle(
        chat,
        store=store
        if store is not None
        else PersistenceSubAgentStore(chat.persistence),
        clock=clock,
    )
    return lifecycle.reconcile_stranded(workspace_id)


__all__ = [
    "SubAgentLifecycle",
    "SubAgentLifecycleError",
    "reconcile_stranded_sub_agents",
]
