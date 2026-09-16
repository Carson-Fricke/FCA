"""Durable sub-agent lifecycle: spawn, input, complete, fail, and cancel.

The lifecycle owns the deterministic guards the specification requires: chat and
workspace scope inheritance, maximum depth, maximum active children, delegated
budget inheritance, and the one-shot input rule.  Records persist through the
chat persistence port, so a sub-agent spawned from a chat or a workflow phase is
equally durable and recoverable.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType

from thesis.agent.runtime.specs import (
    DEFAULT_MAX_CHILDREN,
    DEFAULT_MAX_DEPTH,
    SubAgentSpec,
)
from thesis.agent.runtime.store import SubAgentStore
from thesis.chat.contracts import (
    ChatContractError,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
    replace_record,
)
from thesis.chat.service import ChatService

_TERMINAL_STATUSES = frozenset(
    {
        SubAgentStatus.COMPLETED,
        SubAgentStatus.FAILED,
        SubAgentStatus.CANCELLED,
    }
)


class SubAgentLifecycleError(RuntimeError):
    """Raised when a sub-agent lifecycle command violates its contract."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


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
        """Create one durable child after enforcing every deterministic guard."""
        parent = (
            None
            if parent_sub_agent_id is None
            else self.get(workspace_id, parent_sub_agent_id)
        )
        if parent_sub_agent_id is not None and parent is None:
            raise SubAgentLifecycleError("parent sub-agent does not exist")
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
        siblings = self.children(workspace_id, parent_sub_agent_id)
        active = [
            record for record in siblings if record.status not in _TERMINAL_STATUSES
        ]
        if len(active) >= self._max_children:
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
            spec_version=spec.spec_version,
            kind=spec.kind,
            allowed_tools=spec.allowed_tools,
            status=SubAgentStatus.PENDING,
            accepts_input=False,
            input_contract_ref=spec.input_contract_ref,
            completion_contract_ref=spec.completion_contract_ref,
            delegated_budget=spec.delegated_budget,
            updated_at=now,
        )
        return self._store.save_sub_agent(record)

    def start(self, workspace_id: str, sub_agent_id: str) -> SubAgentRecord:
        return self._update(
            workspace_id,
            sub_agent_id,
            status=SubAgentStatus.RUNNING,
            accepts_input=False,
        )

    def request_input(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        accepts_input: bool = True,
    ) -> SubAgentRecord:
        """Move a node to ``needs_input`` so an operator reply may be accepted."""
        return self._update(
            workspace_id,
            sub_agent_id,
            status=SubAgentStatus.NEEDS_INPUT,
            accepts_input=accepts_input,
        )

    def resume(self, workspace_id: str, sub_agent_id: str) -> SubAgentRecord:
        """Consume operator input and return the node to ``running``."""
        return self._update(
            workspace_id,
            sub_agent_id,
            status=SubAgentStatus.RUNNING,
            accepts_input=False,
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


__all__ = [
    "SubAgentLifecycle",
    "SubAgentLifecycleError",
]
