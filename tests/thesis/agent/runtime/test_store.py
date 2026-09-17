"""Durable sub-agent and invocation store behaviour."""

from __future__ import annotations

from datetime import UTC, datetime

from thesis.agent.runtime.store import (
    InMemorySubAgentStore,
    PersistenceSubAgentStore,
    SubAgentStore,
)
from thesis.chat.contracts import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
)
from thesis.chat.service import ChatService, InMemoryChatPersistence

_WORKSPACE = "ws-test"
_CHAT = "chat-1"
_ACTOR = "operator-1"


def _invocation(invocation_id: str, idempotency_key: str) -> AgentInvocation:
    now = datetime.now(UTC)
    return AgentInvocation(
        workspace_id=_WORKSPACE,
        created_at=now,
        created_by=_ACTOR,
        invocation_id=invocation_id,
        chat_id=_CHAT,
        conversation_id=_CHAT,
        kind=AgentInvocationKind.TOOL,
        state=AgentInvocationState.SUCCEEDED,
        tool_name="run_backtest",
        idempotency_key=idempotency_key,
        updated_at=now,
    )


def _assert_idempotency_lookup(store: SubAgentStore) -> None:
    store.save_invocation(_invocation("inv-1", "key-1"))
    store.save_invocation(_invocation("inv-2", "key-2"))

    found = store.find_invocation_by_idempotency_key(_WORKSPACE, "key-2")

    assert found is not None
    assert found.invocation_id == "inv-2"
    assert store.find_invocation_by_idempotency_key(_WORKSPACE, "missing") is None
    assert store.find_invocation_by_idempotency_key("other-ws", "key-1") is None


def test_in_memory_store_finds_invocations_by_idempotency_key() -> None:
    _assert_idempotency_lookup(InMemorySubAgentStore())


def test_persistence_store_finds_invocations_by_idempotency_key() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())

    _assert_idempotency_lookup(PersistenceSubAgentStore(chat.persistence))
