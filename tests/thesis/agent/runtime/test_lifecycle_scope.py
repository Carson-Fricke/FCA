"""The spawn concurrency guard is scoped to one conversation."""

from __future__ import annotations

import pytest

from thesis.agent.runtime.lifecycle import SubAgentLifecycle, SubAgentLifecycleError
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import InMemorySubAgentStore
from thesis.chat.service import ChatService, InMemoryChatPersistence

_WORKSPACE = "ws-test"
_ACTOR = "operator-1"


def _lifecycle(max_children: int) -> tuple[ChatService, SubAgentLifecycle]:
    chat = ChatService(persistence=InMemoryChatPersistence())
    lifecycle = SubAgentLifecycle(
        chat, store=InMemorySubAgentStore(), max_children=max_children
    )
    return chat, lifecycle


def _chat(chat: ChatService, title: str) -> str:
    return chat.create_chat(_WORKSPACE, title=title, owner_actor_id=_ACTOR).chat_id


def _spawn(
    lifecycle: SubAgentLifecycle,
    chat_id: str,
    *,
    parent_sub_agent_id: str | None = None,
) -> str:
    record = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research"),
        actor_id=_ACTOR,
        parent_sub_agent_id=parent_sub_agent_id,
    )
    return record.sub_agent_id


def test_active_children_limit_is_per_conversation() -> None:
    chat, lifecycle = _lifecycle(2)
    first = _chat(chat, "first")
    second = _chat(chat, "second")
    _spawn(lifecycle, first)
    _spawn(lifecycle, first)

    with pytest.raises(SubAgentLifecycleError, match="maximum of 2"):
        _spawn(lifecycle, first)

    # A different conversation has its own budget and is not blocked by the
    # saturated one.
    spawned = _spawn(lifecycle, second)
    assert spawned


def test_a_parent_must_belong_to_the_same_conversation() -> None:
    chat, lifecycle = _lifecycle(4)
    first = _chat(chat, "first")
    second = _chat(chat, "second")
    parent = _spawn(lifecycle, first)

    with pytest.raises(SubAgentLifecycleError, match="another chat"):
        _spawn(lifecycle, second, parent_sub_agent_id=parent)

    child = _spawn(lifecycle, first, parent_sub_agent_id=parent)
    assert child
