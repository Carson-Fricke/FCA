"""Child conversation semantics: the return layer over one conversation.

A child is a conversation plus child semantics, by composition: it delegates
every internal to its inner conversation and adds only the return anchor, the
return operations, and one turn-end policy.  These tests prove the layer
without a workflow: a one-shot child completes on its final answer and
delivers it to the return anchor; a conversational child stays open; and
every explicit return operation delivers at the anchor it was given.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.delivery import Anchor
from thesis.agent.runtime.session import SessionToolRegistry
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat import SubAgentKind, SubAgentStatus
from thesis.chat.service import ChatService, InMemoryChatPersistence
from thesis.providers.invocations import (
    ChatMessage as ProviderChatMessage,
)
from thesis.providers.invocations import (
    ModelCompletion,
)

_WORKSPACE = "ws-child"
_ACTOR = "operator-1"


class _TextProvider:
    """A completion provider that answers one final text turn."""

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: object = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        del messages, tools, response_format
        return ModelCompletion(
            content="the answer",
            model="stub",
            provider="stub",
            role="primary",
            prompt_tokens=0,
            completion_tokens=0,
        )


def _service(
    *, spawn_thread=None
) -> tuple[Conversations, ChatService, PersistenceSubAgentStore, list]:
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat.create_chat(_WORKSPACE, title="Child chat", owner_actor_id=_ACTOR)
    store = PersistenceSubAgentStore(chat.persistence)
    woke: list[object] = []
    conversations = Conversations(
        chat=chat,
        store=store,
        provider=_TextProvider(),
        registry_factory=lambda context: SessionToolRegistry(),
        spawn_thread=spawn_thread or (lambda target: woke.append(target)),
    )
    return conversations, chat, store, woke


def test_spawn_prompts_the_child_and_wakes_its_own_turn() -> None:
    conversations, chat, store, woke = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    parent = conversations.lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="lead", kind=SubAgentKind.CONVERSATION),
        actor_id=_ACTOR,
    )

    child = conversations.spawn(
        Anchor(_WORKSPACE, chat_id, sub_agent_id=parent.sub_agent_id),
        SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        "do the work",
        SubAgentKind.ONE_SHOT,
        actor_id=_ACTOR,
    )

    assert child.returns_to.sub_agent_id == parent.sub_agent_id
    record = store.get_sub_agent(_WORKSPACE, child.conversation_id)
    assert record is not None
    assert record.parent_sub_agent_id == parent.sub_agent_id
    prompt = child.transcript()
    assert [message.body for message in prompt] == ["do the work"]
    assert prompt[0].data is not None
    assert prompt[0].data["kind"] == "agent_prompt"
    # The prompt delivery is the wake: the child's own turn was scheduled.
    assert len(woke) == 1


def test_a_one_shot_child_completes_on_its_final_answer() -> None:
    conversations, chat, store, woke = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    parent = conversations.lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="lead", kind=SubAgentKind.CONVERSATION),
        actor_id=_ACTOR,
    )
    child = conversations.spawn(
        Anchor(_WORKSPACE, chat_id, sub_agent_id=parent.sub_agent_id),
        SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        "do the work",
        SubAgentKind.ONE_SHOT,
        actor_id=_ACTOR,
    )

    result = child.run_turn()

    assert result is not None
    record = store.get_sub_agent(_WORKSPACE, child.conversation_id)
    assert record is not None
    assert record.status is SubAgentStatus.COMPLETED
    assert record.result == {"summary": "the answer"}
    delivered = [
        message
        for message in chat.list_messages(_WORKSPACE, chat_id)
        if message.data is not None
        and message.data.get("childSubAgentId") == child.conversation_id
    ]
    assert len(delivered) == 1
    assert delivered[0].data is not None
    assert delivered[0].data["subAgentId"] == parent.sub_agent_id
    # The result delivery woke the parent's conversation.
    assert len(woke) == 2


def test_a_conversational_child_stays_open_after_its_answer() -> None:
    conversations, chat, store, woke = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    parent = conversations.lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="lead", kind=SubAgentKind.CONVERSATION),
        actor_id=_ACTOR,
    )
    child = conversations.spawn(
        Anchor(_WORKSPACE, chat_id, sub_agent_id=parent.sub_agent_id),
        SubAgentSpec(role="research", kind=SubAgentKind.CONVERSATION),
        "draft the analysis",
        SubAgentKind.CONVERSATION,
        actor_id=_ACTOR,
    )

    child.run_turn()

    record = store.get_sub_agent(_WORKSPACE, child.conversation_id)
    assert record is not None
    assert record.status is SubAgentStatus.RUNNING
    assert not any(
        message.data is not None
        and message.data.get("childSubAgentId") == child.conversation_id
        for message in chat.list_messages(_WORKSPACE, chat_id)
    )


def test_explicit_completion_delivers_at_the_return_anchor() -> None:
    conversations, chat, store, woke = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    child = conversations.spawn(
        Anchor(_WORKSPACE, chat_id),
        SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        "do the work",
        SubAgentKind.ONE_SHOT,
        actor_id=_ACTOR,
    )

    completed = child.complete({"verdict": "supported"})

    assert completed.status is SubAgentStatus.COMPLETED
    assert completed.result == {"verdict": "supported"}
    assert child.result == {"verdict": "supported"}
    delivered = [
        message
        for message in chat.list_messages(_WORKSPACE, chat_id)
        if message.data is not None
        and message.data.get("childSubAgentId") == child.conversation_id
    ]
    assert len(delivered) == 1
    # A chat-level child returns to the root anchor: the chat's conversation.
    assert delivered[0].data is not None
    assert delivered[0].data["subAgentId"] is None
    assert len(woke) == 2


def test_fail_delivers_the_failure_at_the_return_anchor() -> None:
    conversations, chat, store, woke = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    child = conversations.spawn(
        Anchor(_WORKSPACE, chat_id),
        SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        "do the work",
        SubAgentKind.ONE_SHOT,
        actor_id=_ACTOR,
    )

    failed = child.fail("the tool exploded")

    assert failed.status is SubAgentStatus.FAILED
    assert failed.result == {"failure": "the tool exploded"}
    assert any(
        message.data is not None
        and message.data.get("childSubAgentId") == child.conversation_id
        and message.data.get("status") == "failed"
        for message in chat.list_messages(_WORKSPACE, chat_id)
    )


def test_await_result_returns_the_terminal_state_after_a_bounded_wait() -> None:
    conversations, chat, store, woke = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    child = conversations.spawn(
        Anchor(_WORKSPACE, chat_id),
        SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        "do the work",
        SubAgentKind.ONE_SHOT,
        actor_id=_ACTOR,
    )

    running = child.await_result(timeout_seconds=0.0)

    assert running.terminal is False
    assert running.status == "pending"
    child.complete({"summary": "done"})
    done = child.await_result(timeout_seconds=0.0)
    assert done.terminal is True
    assert done.result == {"summary": "done"}


def test_children_and_cancel_cover_the_record_tree() -> None:
    conversations, chat, store, woke = _service()
    chat_id = chat.list_chats(_WORKSPACE)[0].chat_id
    child = conversations.spawn(
        Anchor(_WORKSPACE, chat_id),
        SubAgentSpec(role="research", kind=SubAgentKind.ONE_SHOT),
        "do the work",
        SubAgentKind.ONE_SHOT,
        actor_id=_ACTOR,
    )
    grandchild = conversations.spawn(
        Anchor(_WORKSPACE, chat_id, sub_agent_id=child.conversation_id),
        SubAgentSpec(role="helper", kind=SubAgentKind.ONE_SHOT),
        "assist",
        SubAgentKind.ONE_SHOT,
        actor_id=_ACTOR,
    )

    assert [record.sub_agent_id for record in child.children()] == [
        grandchild.conversation_id
    ]
    chat_level = conversations.children(_WORKSPACE, chat_id)
    assert [record.sub_agent_id for record in chat_level] == [child.conversation_id]

    cancelled = child.cancel()

    assert set(cancelled) == {child.conversation_id, grandchild.conversation_id}
    record = store.get_sub_agent(_WORKSPACE, child.conversation_id)
    assert record is not None
    assert record.status is SubAgentStatus.CANCELLED
