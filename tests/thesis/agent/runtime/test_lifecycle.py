"""Deterministic lifecycle guards for durable sub-agents."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thesis.agent.runtime.lifecycle import (
    SubAgentLifecycle,
    SubAgentLifecycleError,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import (
    SubAgentKind,
    SubAgentStatus,
    WorkflowExecution,
)
from thesis.chat.service import ChatService, InMemoryChatPersistence
from thesis.execution.events import ExecutionEventType

_WORKSPACE = "ws-test"
_ACTOR = "operator-1"


def _chat() -> ChatService:
    service = ChatService(
        persistence=InMemoryChatPersistence(), id_factory=lambda prefix: f"{prefix}-1"
    )
    service.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    return service


def _lifecycle(
    chat: ChatService,
    *,
    max_depth: int = 4,
    max_children: int = 3,
) -> SubAgentLifecycle:
    store = PersistenceSubAgentStore(chat.persistence)
    return SubAgentLifecycle(
        chat,
        store=store,
        max_depth=max_depth,
        max_children=max_children,
    )


def test_spawn_persists_a_pending_child() -> None:
    chat = _chat()
    lifecycle = _lifecycle(chat)
    child = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research"),
        actor_id=_ACTOR,
    )
    assert child.status is SubAgentStatus.PENDING
    stored = lifecycle.get(_WORKSPACE, child.sub_agent_id)
    assert stored is not None and stored.sub_agent_id == child.sub_agent_id


def test_depth_guard_blocks_too_deep_spawn() -> None:
    chat = _chat()
    lifecycle = _lifecycle(chat, max_depth=1)
    root = lifecycle.spawn(
        _WORKSPACE, "chat-1", spec=SubAgentSpec(role="research"), actor_id=_ACTOR
    )
    child = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research"),
        actor_id=_ACTOR,
        parent_sub_agent_id=root.sub_agent_id,
    )
    with pytest.raises(SubAgentLifecycleError):
        lifecycle.spawn(
            _WORKSPACE,
            "chat-1",
            spec=SubAgentSpec(role="research"),
            actor_id=_ACTOR,
            parent_sub_agent_id=child.sub_agent_id,
        )


def test_active_child_cap_is_enforced() -> None:
    chat = _chat()
    lifecycle = _lifecycle(chat, max_children=1)
    root = lifecycle.spawn(
        _WORKSPACE, "chat-1", spec=SubAgentSpec(role="research"), actor_id=_ACTOR
    )
    lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research"),
        actor_id=_ACTOR,
        parent_sub_agent_id=root.sub_agent_id,
    )
    with pytest.raises(SubAgentLifecycleError):
        lifecycle.spawn(
            _WORKSPACE,
            "chat-1",
            spec=SubAgentSpec(role="research"),
            actor_id=_ACTOR,
            parent_sub_agent_id=root.sub_agent_id,
        )


def test_paused_child_does_not_hold_a_concurrency_slot() -> None:
    chat = _chat()
    lifecycle = _lifecycle(chat, max_children=1)
    root = lifecycle.spawn(
        _WORKSPACE, "chat-1", spec=SubAgentSpec(role="research"), actor_id=_ACTOR
    )
    waiting = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research"),
        actor_id=_ACTOR,
        parent_sub_agent_id=root.sub_agent_id,
    )
    lifecycle.start(_WORKSPACE, waiting.sub_agent_id)
    paused = lifecycle.request_input(_WORKSPACE, waiting.sub_agent_id)
    assert paused.status is SubAgentStatus.NEEDS_INPUT

    # A node paused for operator review is not in flight, so a new child can
    # take the slot even though the cap is one.
    replacement = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research"),
        actor_id=_ACTOR,
        parent_sub_agent_id=root.sub_agent_id,
    )
    assert replacement.status is SubAgentStatus.PENDING

    # The pending replacement now holds the slot and blocks the next spawn.
    with pytest.raises(SubAgentLifecycleError, match="maximum of 1"):
        lifecycle.spawn(
            _WORKSPACE,
            "chat-1",
            spec=SubAgentSpec(role="research"),
            actor_id=_ACTOR,
            parent_sub_agent_id=root.sub_agent_id,
        )


def test_delegated_budget_cannot_exceed_parent() -> None:
    chat = _chat()
    lifecycle = _lifecycle(chat, max_children=8)
    root = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research", delegated_budget=10),
        actor_id=_ACTOR,
    )
    with pytest.raises(SubAgentLifecycleError):
        lifecycle.spawn(
            _WORKSPACE,
            "chat-1",
            spec=SubAgentSpec(role="research", delegated_budget=11),
            actor_id=_ACTOR,
            parent_sub_agent_id=root.sub_agent_id,
        )


def test_conversation_accepts_unsolicited_input_but_one_shot_does_not() -> None:
    chat = _chat()
    lifecycle = _lifecycle(chat, max_children=8)
    conversation = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research", kind=SubAgentKind.CONVERSATION),
        actor_id=_ACTOR,
    )
    one_shot = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, conversation.sub_agent_id)
    lifecycle.start(_WORKSPACE, one_shot.sub_agent_id)
    conversation_record = lifecycle.get(_WORKSPACE, conversation.sub_agent_id)
    one_shot_record = lifecycle.get(_WORKSPACE, one_shot.sub_agent_id)
    assert conversation_record is not None
    assert one_shot_record is not None
    assert lifecycle.accepts_unsolicited_input(conversation_record)
    assert not lifecycle.accepts_unsolicited_input(one_shot_record)


def test_request_input_then_complete_is_terminal() -> None:
    chat = _chat()
    lifecycle = _lifecycle(chat)
    node = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, node.sub_agent_id)
    waiting = lifecycle.request_input(_WORKSPACE, node.sub_agent_id)
    assert waiting.status is SubAgentStatus.NEEDS_INPUT
    assert lifecycle.accepts_unsolicited_input(waiting)
    done = lifecycle.complete(
        _WORKSPACE, node.sub_agent_id, result={"verdict": "supported"}
    )
    assert done.status is SubAgentStatus.COMPLETED
    assert not lifecycle.accepts_unsolicited_input(done)
    with pytest.raises(SubAgentLifecycleError):
        lifecycle.start(_WORKSPACE, node.sub_agent_id)


@pytest.mark.parametrize("kind", [SubAgentKind.CONVERSATION, SubAgentKind.ONE_SHOT])
def test_answer_keeps_conversation_input_open_but_not_one_shot(
    kind: SubAgentKind,
) -> None:
    lifecycle = _lifecycle(_chat())
    node = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research", kind=kind),
        actor_id=_ACTOR,
    )
    lifecycle.start(_WORKSPACE, node.sub_agent_id)
    lifecycle.request_input(_WORKSPACE, node.sub_agent_id)

    resumed = lifecycle.resume(_WORKSPACE, node.sub_agent_id)

    assert resumed.status is SubAgentStatus.RUNNING
    assert resumed.accepts_input is (kind is SubAgentKind.CONVERSATION)
    assert lifecycle.accepts_unsolicited_input(resumed) is resumed.accepts_input


def _execution(chat: ChatService, execution_id: str, chat_id: str) -> None:
    chat.persistence.save(
        WorkflowExecution(
            workspace_id=_WORKSPACE,
            execution_id=execution_id,
            chat_id=chat_id,
            trigger_message_id="msg-1",
            workflow_id="hypothesis-development",
            workflow_version="1.0.0",
            requested_by=_ACTOR,
            created_at=datetime.now(UTC),
            created_by=_ACTOR,
        )
    )


def test_request_input_projects_a_needs_input_event_for_execution_nodes() -> None:
    chat = _chat()
    lifecycle = _lifecycle(chat)
    _execution(chat, "exec-1", "chat-1")
    node = lifecycle.spawn(
        _WORKSPACE,
        "chat-1",
        spec=SubAgentSpec(role="research"),
        actor_id=_ACTOR,
        workflow_execution_id="exec-1",
    )
    lifecycle.start(_WORKSPACE, node.sub_agent_id)
    lifecycle.request_input(_WORKSPACE, node.sub_agent_id)

    events = chat.replay_events(_WORKSPACE, "exec-1").events
    needs_input = [
        event
        for event in events
        if event.event_type is ExecutionEventType.SUB_AGENT_NEEDS_INPUT
    ]
    assert len(needs_input) == 1
    payload = needs_input[0].payload
    assert payload["subAgentId"] == node.sub_agent_id
    assert payload["acceptsInput"] is True
    assert payload["phase"] == ""


def test_request_input_without_an_execution_appends_no_event() -> None:
    chat = _chat()
    lifecycle = _lifecycle(chat)
    # A sibling execution exists in the log, but the node itself carries no
    # execution attribution: the flip must not project anywhere.
    _execution(chat, "exec-1", "chat-1")
    node = lifecycle.spawn(
        _WORKSPACE, "chat-1", spec=SubAgentSpec(role="research"), actor_id=_ACTOR
    )
    lifecycle.start(_WORKSPACE, node.sub_agent_id)
    lifecycle.request_input(_WORKSPACE, node.sub_agent_id)

    assert chat.replay_events(_WORKSPACE, "exec-1").events == ()
