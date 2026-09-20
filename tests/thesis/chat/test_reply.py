"""Focused tests for streamed chat reply generation."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from thesis.agent.runtime.conversation import Conversations
from thesis.chat import (
    CHAT_MESSAGE,
    PROVIDER_DELTA,
    ChatMessage,
    ChatService,
    InMemoryChatStreamBroker,
)
from thesis.chat.contracts import MessageRole
from thesis.chat.responder import ReplyChunk

WORKSPACE = "workspace-a"


def _reply_provider(*chunks: str):
    def stream(history: Sequence[ChatMessage]) -> Iterator[str]:
        del history
        yield from chunks

    return stream


def _reasoning_provider(reasoning: str, answer: str):
    def stream(history: Sequence[ChatMessage]) -> Iterator[str | ReplyChunk]:
        del history
        yield ReplyChunk(text=reasoning, kind="reasoning")
        yield answer

    return stream


def _conversations(
    service: ChatService,
    broker: InMemoryChatStreamBroker,
    stream,
    *,
    spawn_thread=None,
) -> Conversations:
    return Conversations(
        chat=service,
        broker=broker,
        reply_for=lambda workspace_id, chat_id, history: stream(history),
        spawn_thread=spawn_thread,
    )


def test_reply_source_receives_the_turn_history() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")
    captured: dict[str, object] = {}
    captured_history: list[ChatMessage] = []

    def source(
        workspace_id: str, chat_id: str, history: Sequence[ChatMessage]
    ) -> Iterator[str]:
        captured["workspace_id"] = workspace_id
        captured["chat_id"] = chat_id
        captured_history.extend(history)
        yield "ok"

    conversations = Conversations(chat=service, broker=broker, reply_for=source)

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert message is not None
    assert captured["workspace_id"] == WORKSPACE
    assert captured["chat_id"] == chat.chat_id
    assert [item.role for item in captured_history] == [MessageRole.USER]


def test_node_turn_never_enters_the_top_level_history() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="root")
    service.create_message(
        WORKSPACE,
        chat.chat_id,
        actor_id="research-agent",
        body="node-only",
        role=MessageRole.USER,
        data={"subAgentId": "node-1"},
    )
    captured: list[ChatMessage] = []

    def source(
        workspace_id: str, chat_id: str, history: Sequence[ChatMessage]
    ) -> Iterator[str]:
        del workspace_id, chat_id
        captured.extend(history)
        yield "ok"

    conversations = Conversations(chat=service, broker=broker, reply_for=source)

    conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert [message.body for message in captured] == ["root"]


def test_reply_streams_deltas_and_persists_assistant_message() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")
    conversations = _conversations(service, broker, _reply_provider("Hel", "lo!"))

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert message is not None
    assert message.role is MessageRole.ASSISTANT
    assert message.body == "Hello!"
    frames = broker.read(WORKSPACE, chat.chat_id)
    assert frames[0].event == PROVIDER_DELTA
    assert frames[0].data["start"] is True
    assert frames[-2].event == CHAT_MESSAGE
    assert frames[-1].data["messageIdFinal"] == message.message_id
    stored = service.list_messages(WORKSPACE, chat.chat_id)
    assert [message.role for message in stored] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]


def test_reply_failure_streams_error_and_persists_nothing() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")

    def failing(history: Sequence[ChatMessage]) -> Iterator[str]:
        del history
        raise RuntimeError("provider down")

    conversations = _conversations(service, broker, failing)

    assert conversations.root(WORKSPACE, chat.chat_id).run_turn() is None
    frames = broker.read(WORKSPACE, chat.chat_id)
    assert any(frame.data.get("error") == "provider down" for frame in frames)
    stored = service.list_messages(WORKSPACE, chat.chat_id)
    assert [message.role for message in stored] == [MessageRole.USER]


def test_reply_persists_multiline_text() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")
    conversations = _conversations(
        service, broker, _reply_provider("Line one\n\nLine two — done ✓")
    )

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert message is not None
    assert message.body == "Line one\n\nLine two — done ✓"


def test_reply_sanitizes_control_characters() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")
    conversations = _conversations(
        service, broker, _reply_provider("ok\x1b[31mred\x00")
    )

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert message is not None
    assert message.body == "ok[31mred"


def test_reply_streams_reasoning_before_answer_on_a_separate_channel() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")
    conversations = _conversations(
        service, broker, _reasoning_provider("Weigh the options.", "Answer text.")
    )

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert message is not None
    assert message.body == "Answer text."
    assert message.reasoning == "Weigh the options."
    deltas = [
        frame
        for frame in broker.read(WORKSPACE, chat.chat_id)
        if frame.event == PROVIDER_DELTA and frame.data.get("text")
    ]
    kinds = [frame.data.get("kind") for frame in deltas]
    assert kinds == ["reasoning", None]
    assert deltas[0].data["text"] == "Weigh the options."
    assert deltas[1].data["text"] == "Answer text."
    frames = broker.read(WORKSPACE, chat.chat_id)
    chat_frame = next(frame for frame in frames if frame.event == CHAT_MESSAGE)
    persisted = chat_frame.data["message"]
    assert isinstance(persisted, dict)
    assert persisted["body"] == "Answer text."
    assert persisted["reasoning"] == "Weigh the options."


def test_send_persists_the_message_and_wakes_the_reply_turn() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    woke: list[object] = []

    def spawn_thread(target) -> None:
        woke.append(target())

    conversations = _conversations(
        service, broker, _reply_provider("Hi there"), spawn_thread=spawn_thread
    )

    message = conversations.root(WORKSPACE, chat.chat_id).send(
        body="Hello", actor_id="operator"
    )

    assert message.role is MessageRole.USER
    assert message.body == "Hello"
    assert len(woke) == 1
    stored = service.list_messages(WORKSPACE, chat.chat_id)
    assert [item.role for item in stored] == [MessageRole.USER, MessageRole.ASSISTANT]


def test_a_record_anchor_delivery_schedules_nothing() -> None:
    from thesis.agent.runtime.specs import SubAgentSpec
    from thesis.chat.contracts import (
        RECORD_ANCHOR_ROLES,
        SubAgentKind,
    )
    from thesis.chat.service import InMemoryChatPersistence

    chat = ChatService(persistence=InMemoryChatPersistence())
    chat_id = chat.create_chat(
        WORKSPACE, title="Anchors", owner_actor_id="operator"
    ).chat_id
    conversations = _conversations(chat, InMemoryChatStreamBroker(), _reply_provider())
    record = conversations.lifecycle.spawn(
        WORKSPACE,
        chat_id,
        spec=SubAgentSpec(
            role="workflow-node",
            kind=SubAgentKind.ONE_SHOT,
        ),
        actor_id="operator",
    )
    assert record.role in RECORD_ANCHOR_ROLES

    conversations.schedule(WORKSPACE, chat_id, record.sub_agent_id)

    assert conversations.in_flight(WORKSPACE, chat_id, record.sub_agent_id) is False
