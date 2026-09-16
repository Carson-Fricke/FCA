"""Contract validation for durable sub-agent and invocation records."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thesis.chat.contracts import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    ChatContractError,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _sub_agent(
    *,
    kind: SubAgentKind = SubAgentKind.CONVERSATION,
    status: SubAgentStatus = SubAgentStatus.PENDING,
    accepts_input: bool = False,
    allowed_tools: tuple[str, ...] = (),
    delegated_budget: int = 0,
    workflow_execution_id: str | None = None,
    phase_attempt_id: str | None = None,
) -> SubAgentRecord:
    return SubAgentRecord(
        workspace_id="ws-test",
        created_at=_NOW,
        created_by="operator-1",
        sub_agent_id="subagent-1",
        chat_id="chat-1",
        role="research",
        kind=kind,
        status=status,
        accepts_input=accepts_input,
        allowed_tools=allowed_tools,
        delegated_budget=delegated_budget,
        workflow_execution_id=workflow_execution_id,
        phase_attempt_id=phase_attempt_id,
    )


def test_one_shot_running_cannot_accept_unsolicited_input() -> None:
    with pytest.raises(ChatContractError):
        _sub_agent(
            kind=SubAgentKind.ONE_SHOT,
            status=SubAgentStatus.RUNNING,
            accepts_input=True,
        )


def test_one_shot_needs_input_may_accept_input() -> None:
    record = _sub_agent(
        kind=SubAgentKind.ONE_SHOT,
        status=SubAgentStatus.NEEDS_INPUT,
        accepts_input=True,
    )
    assert record.accepts_input is True


def test_sub_agent_round_trips_through_payload() -> None:
    from thesis.chat.persistence import record_from_payload, record_payload

    record = _sub_agent(
        kind=SubAgentKind.ONE_SHOT,
        status=SubAgentStatus.NEEDS_INPUT,
        accepts_input=True,
        allowed_tools=("run_backtest", "get_run"),
        delegated_budget=100,
        workflow_execution_id="execution-1",
        phase_attempt_id="attempt-1",
    )
    restored = record_from_payload(SubAgentRecord, record_payload(record))
    assert isinstance(restored, SubAgentRecord)
    assert restored == record


def test_tool_invocation_requires_tool_name() -> None:
    with pytest.raises(ChatContractError):
        AgentInvocation(
            workspace_id="ws-test",
            created_at=_NOW,
            created_by="operator-1",
            invocation_id="invocation-1",
            chat_id="chat-1",
            conversation_id="subagent-1",
            kind=AgentInvocationKind.TOOL,
            state=AgentInvocationState.REQUESTED,
        )


def test_agent_invocation_round_trips_through_payload() -> None:
    from thesis.chat.persistence import record_from_payload, record_payload

    invocation = AgentInvocation(
        workspace_id="ws-test",
        created_at=_NOW,
        created_by="operator-1",
        invocation_id="invocation-1",
        chat_id="chat-1",
        conversation_id="subagent-1",
        kind=AgentInvocationKind.TOOL,
        state=AgentInvocationState.SUCCEEDED,
        sub_agent_id="subagent-1",
        tool_name="spawn_sub_agent",
        input_hash="a" * 64,
        idempotency_key="key-1",
    )
    restored = record_from_payload(AgentInvocation, record_payload(invocation))
    assert isinstance(restored, AgentInvocation)
    assert restored == invocation
