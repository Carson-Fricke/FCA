"""Deterministic lifecycle guards for durable sub-agents."""

from __future__ import annotations

import pytest

from thesis.agent.runtime.lifecycle import (
    SubAgentLifecycle,
    SubAgentLifecycleError,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import SubAgentKind, SubAgentStatus
from thesis.chat.service import ChatService, InMemoryChatPersistence

_WORKSPACE = "ws-test"
_ACTOR = "operator-1"


def _chat() -> ChatService:
    service = ChatService(persistence=InMemoryChatPersistence())
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
