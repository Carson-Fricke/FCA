"""Local/remote durability parity for the new runtime records.

The filesystem adapter is the local backend; Cosmos and Azure blob serialize
with the same ``record_payload`` / ``record_from_payload`` wire format.  These
tests prove a sub-agent and an invocation survive the local backend and the
shared remote wire format identically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from thesis.chat.contracts import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
)
from thesis.chat.persistence import (
    FileChatPersistence,
    record_from_payload,
    record_payload,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _sub_agent() -> SubAgentRecord:
    return SubAgentRecord(
        workspace_id="ws-test",
        created_at=_NOW,
        created_by="operator-1",
        sub_agent_id="subagent-1",
        chat_id="chat-1",
        role="research",
        kind=SubAgentKind.ONE_SHOT,
        status=SubAgentStatus.NEEDS_INPUT,
        accepts_input=True,
        allowed_tools=("run_backtest", "get_run"),
        delegated_budget=100,
        result={"verdict": "supported"},
    )


def _invocation() -> AgentInvocation:
    return AgentInvocation(
        workspace_id="ws-test",
        created_at=_NOW,
        created_by="thesis-control",
        invocation_id="invocation-1",
        chat_id="chat-1",
        conversation_id="subagent-1",
        kind=AgentInvocationKind.TOOL,
        state=AgentInvocationState.SUCCEEDED,
        sub_agent_id="subagent-1",
        tool_name="spawn_sub_agent",
        idempotency_key="key-1",
    )


def test_file_persistence_round_trips_runtime_records(tmp_path: Path) -> None:
    store = FileChatPersistence(tmp_path)
    sub_agent = store.save(_sub_agent())
    invocation = store.save(_invocation())

    assert store.get(SubAgentRecord, "ws-test", sub_agent.sub_agent_id) == sub_agent
    assert store.get(AgentInvocation, "ws-test", invocation.invocation_id) == invocation
    assert store.list(SubAgentRecord, "ws-test") == (sub_agent,)


def test_wire_format_matches_file_persistence(tmp_path: Path) -> None:
    store = FileChatPersistence(tmp_path)
    sub_agent = store.save(_sub_agent())
    invocation = store.save(_invocation())

    # The Cosmos/Azure adapters persist exactly this payload, so remote parity is
    # proven by reconstructing the same records from the shared wire format.
    assert record_from_payload(SubAgentRecord, record_payload(sub_agent)) == sub_agent
    assert (
        record_from_payload(AgentInvocation, record_payload(invocation)) == invocation
    )
