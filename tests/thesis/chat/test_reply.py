"""Focused tests for streamed chat reply generation."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from thesis.chat import (
    CHAT_MESSAGE,
    PROVIDER_DELTA,
    ChatMessage,
    ChatReplyService,
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


def test_reply_streams_deltas_and_persists_assistant_message() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")
    reply = ChatReplyService(
        chat_service=service, broker=broker, provider=_reply_provider("Hel", "lo!")
    )

    message = reply.respond(WORKSPACE, chat.chat_id)

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

    reply = ChatReplyService(chat_service=service, broker=broker, provider=failing)

    assert reply.respond(WORKSPACE, chat.chat_id) is None
    frames = broker.read(WORKSPACE, chat.chat_id)
    assert any(frame.data.get("error") == "provider down" for frame in frames)
    stored = service.list_messages(WORKSPACE, chat.chat_id)
    assert [message.role for message in stored] == [MessageRole.USER]


def test_reply_persists_multiline_text() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")
    reply = ChatReplyService(
        chat_service=service,
        broker=broker,
        provider=_reply_provider("Line one\n\nLine two — done ✓"),
    )

    message = reply.respond(WORKSPACE, chat.chat_id)

    assert message is not None
    assert message.body == "Line one\n\nLine two — done ✓"


def test_reply_sanitizes_control_characters() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")
    reply = ChatReplyService(
        chat_service=service,
        broker=broker,
        provider=_reply_provider("ok\x1b[31mred\x00"),
    )

    message = reply.respond(WORKSPACE, chat.chat_id)

    assert message is not None
    assert message.body == "ok[31mred"


def test_reply_streams_reasoning_before_answer_on_a_separate_channel() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body="Hi")
    reply = ChatReplyService(
        chat_service=service,
        broker=broker,
        provider=_reasoning_provider("Weigh the options.", "Answer text."),
    )

    message = reply.respond(WORKSPACE, chat.chat_id)

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
