"""Focused coverage for persisted tool-result messages in chat replies.

A tool outcome is a first-class conversation entity, not answer text: these
tests prove a :class:`~thesis.chat.responder.ToolResultChunk` persists a
``tool``-role message carrying the structured result at its chronological
position, publishes it as an ordered ``chat.message`` frame, and leaves the
persisted assistant body and reasoning channels untouched.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from thesis.agent.runtime.conversation import Conversations
from thesis.chat import (
    CHAT_MESSAGE,
    ChatMessage,
    ChatService,
    InMemoryChatStreamBroker,
)
from thesis.chat.contracts import MessageRole
from thesis.chat.responder import ToolResultChunk

WORKSPACE = "workspace-a"


def _thread(service: ChatService, body: str = "run it"):
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    service.create_message(WORKSPACE, chat.chat_id, actor_id="operator", body=body)
    return chat


def _conversations(
    service: ChatService, broker: InMemoryChatStreamBroker, stream
) -> Conversations:
    return Conversations(
        chat=service,
        broker=broker,
        reply_for=lambda workspace_id, chat_id, history: stream(history),
    )


def test_tool_result_is_persisted_as_an_ordered_message() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = _thread(service)

    def stream(
        history: Sequence[ChatMessage],
    ) -> Iterator[str | ToolResultChunk]:
        del history
        yield ToolResultChunk(
            tool="run_backtest",
            call_id="call-1",
            ok=True,
            summary="run_backtest completed",
            structured={"jobId": "job-1"},
            text="ok",
        )
        yield "Answer text."

    conversations = _conversations(service, broker, stream)

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert message is not None
    assert message.body == "Answer text."
    stored = service.list_messages(WORKSPACE, chat.chat_id)
    assert [item.role for item in stored] == [
        MessageRole.USER,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    tool_message = stored[1]
    assert tool_message.body == "run_backtest completed"
    assert tool_message.tool_result == {
        "tool": "run_backtest",
        "callId": "call-1",
        "ok": True,
        "summary": "run_backtest completed",
        "structured": {"jobId": "job-1"},
        "text": "ok",
        "failure": None,
    }
    chat_frames = [
        frame
        for frame in broker.read(WORKSPACE, chat.chat_id)
        if frame.event == CHAT_MESSAGE
    ]
    published_roles: list[object] = []
    for frame in chat_frames:
        payload = frame.data["message"]
        assert isinstance(payload, dict)
        published_roles.append(payload["role"])
    assert published_roles == ["tool", "assistant"]


def test_tool_failure_is_persisted_without_touching_the_answer() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    chat = _thread(service)

    def stream(
        history: Sequence[ChatMessage],
    ) -> Iterator[str | ToolResultChunk]:
        del history
        yield ToolResultChunk(
            tool="get_run",
            call_id=None,
            ok=False,
            summary="get_run failed",
            failure={
                "kind": "tool",
                "message": "boom",
                "tool": "get_run",
                "statusCode": None,
                "payload": {},
            },
        )
        yield "Recovered."

    conversations = _conversations(service, broker, stream)

    message = conversations.root(WORKSPACE, chat.chat_id).run_turn()

    assert message is not None
    assert message.body == "Recovered."
    tool_message = next(
        item
        for item in service.list_messages(WORKSPACE, chat.chat_id)
        if item.role is MessageRole.TOOL
    )
    result = tool_message.tool_result
    assert result is not None
    assert result["ok"] is False
    failure = result["failure"]
    assert isinstance(failure, dict)
    assert failure["kind"] == "tool"
    assert "boom" not in message.body
