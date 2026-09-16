"""Asynchronous child-result delivery to a parent conversation."""

from __future__ import annotations

from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.notify import SubAgentNotifier
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import MessageRole, SubAgentKind, TranscriptItemKind
from thesis.chat.service import ChatService, InMemoryChatPersistence

_WORKSPACE = "ws-test"
_ACTOR = "operator-1"


def _setup():
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    scheduled: list[tuple[str, str, str]] = []
    notifier = SubAgentNotifier(
        chat, schedule=lambda ws, chat_id, node: scheduled.append((ws, chat_id, node))
    )
    return chat, thread.chat_id, lifecycle, notifier, scheduled


def test_root_sub_agent_has_no_parent_notification() -> None:
    _, chat_id, lifecycle, notifier, scheduled = _setup()
    root = lifecycle.spawn(
        _WORKSPACE, chat_id, spec=SubAgentSpec(role="research"), actor_id=_ACTOR
    )
    assert notifier.notify(root) is None
    assert scheduled == []


def test_child_completion_notifies_and_schedules_the_parent() -> None:
    _, chat_id, lifecycle, notifier, scheduled = _setup()
    root = lifecycle.spawn(
        _WORKSPACE, chat_id, spec=SubAgentSpec(role="research"), actor_id=_ACTOR
    )
    child = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="critique", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
        parent_sub_agent_id=root.sub_agent_id,
    )
    lifecycle.start(_WORKSPACE, child.sub_agent_id)
    completed = lifecycle.complete(
        _WORKSPACE, child.sub_agent_id, result={"finding": "no regularity"}
    )
    message = notifier.notify(completed)
    assert message is not None
    assert message.role is MessageRole.TOOL
    assert message.kind is TranscriptItemKind.SUB_AGENT
    assert message.data is not None
    assert message.data["subAgentId"] == root.sub_agent_id
    assert message.data["childSubAgentId"] == child.sub_agent_id
    assert message.data["kind"] == "child_result"
    assert scheduled == [(_WORKSPACE, chat_id, root.sub_agent_id)]
