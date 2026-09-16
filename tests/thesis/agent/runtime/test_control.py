"""Phase-completion handshake and control-tool command behavior."""

from __future__ import annotations

import pytest

from thesis.agent.runtime.control import (
    TOOL_SPAWN_SUB_AGENT,
    AwaitResult,
    CompletionStatus,
    ControlToolService,
    PhaseCompletionProtocol,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import AgentInvocation, SubAgentKind
from thesis.chat.service import ChatService, InMemoryChatPersistence

_WORKSPACE = "ws-test"
_ACTOR = "operator-1"


def _construct(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or "claim" not in payload:
        raise ValueError("claim is required")
    return payload


def test_protocol_accepts_a_valid_contract() -> None:
    protocol = PhaseCompletionProtocol(phase_attempt_id="attempt-1")
    outcome = protocol.submit({"claim": "x"}, _construct)
    assert outcome.status is CompletionStatus.CONSTRUCTED
    assert outcome.accepted
    assert outcome.value == {"claim": "x"}


def test_protocol_reports_construction_failures_in_turn_then_exhausts() -> None:
    protocol = PhaseCompletionProtocol(phase_attempt_id="attempt-1")
    first = protocol.submit({}, _construct)
    second = protocol.submit({}, _construct)
    third = protocol.submit({}, _construct)
    assert first.status is CompletionStatus.CONSTRUCTION_FAILED
    assert first.errors and "claim is required" in first.errors[0]
    assert second.status is CompletionStatus.CONSTRUCTION_FAILED
    assert third.status is CompletionStatus.EXHAUSTED
    blocked = protocol.submit({"claim": "x"}, _construct)
    assert blocked.status is CompletionStatus.EXHAUSTED


def test_protocol_allows_two_repair_turns_then_escalates() -> None:
    protocol = PhaseCompletionProtocol(phase_attempt_id="attempt-1")
    protocol.submit({}, _construct)
    first = protocol.begin_repair_turn()
    assert first.status is CompletionStatus.REPAIR
    assert first.repair_turn == 1
    assert protocol.attempts_used == 0
    second = protocol.begin_repair_turn()
    assert second.status is CompletionStatus.REPAIR
    assert second.repair_turn == 2
    escalated = protocol.begin_repair_turn()
    assert escalated.status is CompletionStatus.ESCALATE


def test_reset_epoch_restores_budgets() -> None:
    protocol = PhaseCompletionProtocol(phase_attempt_id="attempt-1")
    protocol.submit({}, _construct)
    protocol.begin_repair_turn()
    protocol.begin_repair_turn()
    assert protocol.begin_repair_turn().status is CompletionStatus.ESCALATE
    protocol.reset_epoch()
    assert protocol.epoch == 1
    assert protocol.repair_turn == 0
    assert protocol.submit({"claim": "x"}, _construct).accepted


def _service() -> tuple[ControlToolService, SubAgentLifecycle, ChatService]:
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    service = ControlToolService(lifecycle=lifecycle, store=store)
    return service, lifecycle, chat


def test_spawn_records_a_first_class_tool_invocation() -> None:
    service, lifecycle, chat = _service()
    result = service.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
        conversation_id="subagent-root",
        idempotency_key="key-1",
    )
    assert result.sub_agent.parent_sub_agent_id is None
    saved = chat.persistence.get(
        AgentInvocation, _WORKSPACE, result.invocation.invocation_id
    )
    assert saved is not None
    assert saved.tool_name == TOOL_SPAWN_SUB_AGENT
    assert saved.idempotency_key == "key-1"
    assert result.sub_agent.spawned_by_invocation_id == saved.invocation_id


def test_await_returns_non_terminal_then_terminal() -> None:
    service, lifecycle, _ = _service()
    child = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, child.sub_agent_id)
    running: AwaitResult = service.await_sub_agent(_WORKSPACE, child.sub_agent_id)
    assert not running.terminal
    service.complete_sub_agent(
        _WORKSPACE, child.sub_agent_id, result={"verdict": "supported"}
    )
    done = service.await_sub_agent(_WORKSPACE, child.sub_agent_id)
    assert done.terminal
    assert done.result == {"verdict": "supported"}


def test_request_operator_input_pauses_the_node() -> None:
    service, lifecycle, _ = _service()
    child = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, child.sub_agent_id)
    request = service.request_operator_input(
        _WORKSPACE, child.sub_agent_id, message="Confirm the model?", reason="review"
    )
    assert request.message == "Confirm the model?"
    record = lifecycle.get(_WORKSPACE, child.sub_agent_id)
    assert record is not None and record.accepts_input
    with pytest.raises(ValueError):
        service.request_operator_input(_WORKSPACE, child.sub_agent_id, message="   ")
