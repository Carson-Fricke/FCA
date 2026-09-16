"""Durable stores for sub-agent records and first-class invocations.

Both records are ``WorkspaceRecord`` subclasses, so they persist through the
same :class:`~thesis.chat.service.ChatPersistence` port as every other chat
record: the in-memory, filesystem (local), Cosmos, and Azure blob adapters all
carry them without a separate backend.  The store protocol keeps the runtime
independent of the concrete persistence adapter.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from thesis.chat.contracts import AgentInvocation, SubAgentRecord
from thesis.chat.service import ChatPersistence

_SubAgentKey = tuple[str, str]


class SubAgentStore(Protocol):
    """Storage port for durable sub-agent and invocation records."""

    def save_sub_agent(self, record: SubAgentRecord) -> SubAgentRecord: ...

    def get_sub_agent(
        self, workspace_id: str, sub_agent_id: str
    ) -> SubAgentRecord | None: ...

    def list_sub_agents(self, workspace_id: str) -> tuple[SubAgentRecord, ...]: ...

    def save_invocation(self, invocation: AgentInvocation) -> AgentInvocation: ...

    def get_invocation(
        self, workspace_id: str, invocation_id: str
    ) -> AgentInvocation | None: ...


class InMemorySubAgentStore:
    """Deterministic store for tests and process-local composition."""

    def __init__(
        self,
        sub_agents: Iterable[SubAgentRecord] = (),
        invocations: Iterable[AgentInvocation] = (),
    ) -> None:
        self._sub_agents: dict[_SubAgentKey, SubAgentRecord] = {}
        self._invocations: dict[_SubAgentKey, AgentInvocation] = {}
        for record in sub_agents:
            self.save_sub_agent(record)
        for invocation in invocations:
            self.save_invocation(invocation)

    def save_sub_agent(self, record: SubAgentRecord) -> SubAgentRecord:
        self._sub_agents[(record.workspace_id, record.sub_agent_id)] = record
        return record

    def get_sub_agent(
        self, workspace_id: str, sub_agent_id: str
    ) -> SubAgentRecord | None:
        return self._sub_agents.get((workspace_id, sub_agent_id))

    def list_sub_agents(self, workspace_id: str) -> tuple[SubAgentRecord, ...]:
        records = [
            record
            for (stored_workspace, _), record in self._sub_agents.items()
            if stored_workspace == workspace_id
        ]
        records.sort(key=lambda record: (record.created_at, record.sub_agent_id))
        return tuple(records)

    def save_invocation(self, invocation: AgentInvocation) -> AgentInvocation:
        self._invocations[(invocation.workspace_id, invocation.invocation_id)] = (
            invocation
        )
        return invocation

    def get_invocation(
        self, workspace_id: str, invocation_id: str
    ) -> AgentInvocation | None:
        return self._invocations.get((workspace_id, invocation_id))

    def find_invocation_by_idempotency_key(
        self, workspace_id: str, idempotency_key: str
    ) -> AgentInvocation | None:
        for (stored_workspace, _), invocation in self._invocations.items():
            if (
                stored_workspace == workspace_id
                and invocation.idempotency_key == idempotency_key
            ):
                return invocation
        return None


class PersistenceSubAgentStore:
    """Adapter over the shared chat persistence port."""

    def __init__(self, persistence: ChatPersistence) -> None:
        self._persistence = persistence

    def save_sub_agent(self, record: SubAgentRecord) -> SubAgentRecord:
        return self._persistence.save(record)

    def get_sub_agent(
        self, workspace_id: str, sub_agent_id: str
    ) -> SubAgentRecord | None:
        return self._persistence.get(SubAgentRecord, workspace_id, sub_agent_id)

    def list_sub_agents(self, workspace_id: str) -> tuple[SubAgentRecord, ...]:
        return self._persistence.list(SubAgentRecord, workspace_id)

    def save_invocation(self, invocation: AgentInvocation) -> AgentInvocation:
        return self._persistence.save(invocation)

    def get_invocation(
        self, workspace_id: str, invocation_id: str
    ) -> AgentInvocation | None:
        return self._persistence.get(AgentInvocation, workspace_id, invocation_id)


__all__ = [
    "InMemorySubAgentStore",
    "PersistenceSubAgentStore",
    "SubAgentStore",
]
