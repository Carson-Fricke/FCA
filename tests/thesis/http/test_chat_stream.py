"""HTTP tests for chat-scoped streamed replies."""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

from fastapi.testclient import TestClient

from thesis.agent.runtime.conversation import Conversations
from thesis.chat import (
    CHAT_MESSAGE,
    ChatMessage,
    ChatService,
    InMemoryChatStreamBroker,
)
from thesis.chat.responder import ToolResultChunk
from thesis.http.application import create_app

TOKEN = "thesis-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
WORKSPACE = "workspace-a"


async def _no_sleeper(seconds: float) -> None:
    del seconds


def _provider(*chunks: str):
    def stream(history: Sequence[ChatMessage]) -> Iterator[str]:
        del history
        yield from chunks

    return stream


def _conversations(
    service: ChatService, broker: InMemoryChatStreamBroker, stream
) -> Conversations:
    return Conversations(
        chat=service,
        broker=broker,
        reply_for=lambda workspace_id, chat_id, history: stream(history),
    )


def _make_app(service: ChatService, broker: InMemoryChatStreamBroker, conversations):
    return create_app(
        bearer_token=TOKEN,
        chat_service=service,
        chat_stream_broker=broker,
        conversations=conversations,
        stream_sleeper=_no_sleeper,
        stream_max_poll_cycles=1,
    )


def _await_reply(
    conversations: Conversations, workspace_id: str, chat_id: str, timeout: float = 5.0
) -> None:
    """Wait bounded for the root conversation's scheduled turn to finish."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not conversations.in_flight(workspace_id, chat_id, ""):
            return
        time.sleep(0.01)


def test_post_message_schedules_a_streamed_reply() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    conversations = _conversations(service, broker, _provider("Pong"))
    client = TestClient(_make_app(service, broker, conversations))
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=HEADERS,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()

    posted = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages",
        headers=HEADERS,
        json={"actor_id": "operator", "body": "ping"},
    )
    assert posted.status_code == 200

    # The conversation's worker owns the reply turn: it runs out of band from
    # the request, so wait bounded for the turn to land its frames.
    _await_reply(conversations, WORKSPACE, chat["chat_id"])
    frames = broker.read(WORKSPACE, chat["chat_id"])
    assert any(frame.event == CHAT_MESSAGE for frame in frames)
    stored = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages", headers=HEADERS
    ).json()["items"]
    assert [message["role"] for message in stored] == ["user", "assistant"]


def test_chat_stream_route_emits_reply_frames() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    conversations = _conversations(service, broker, _provider("Pong"))
    client = TestClient(_make_app(service, broker, conversations))
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=HEADERS,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    service.create_message(WORKSPACE, chat["chat_id"], actor_id="operator", body="ping")
    conversations.root(WORKSPACE, chat["chat_id"]).run_turn()

    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/events/stream",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "event: provider.delta" in response.text
    assert "event: chat.message" in response.text


def test_chat_stream_route_emits_tool_result_frames() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()

    def stream(
        history: Sequence[ChatMessage],
    ) -> Iterator[str | ToolResultChunk]:
        del history
        yield ToolResultChunk(
            tool="run_backtest",
            call_id="call-1",
            ok=True,
            summary="run_backtest completed",
            structured={"boundary": "local", "exit_code": 0},
            text="ok",
        )
        yield "Done."

    conversations = _conversations(service, broker, stream)
    client = TestClient(_make_app(service, broker, conversations))
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=HEADERS,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    service.create_message(WORKSPACE, chat["chat_id"], actor_id="operator", body="run")
    conversations.root(WORKSPACE, chat["chat_id"]).run_turn()

    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/events/stream",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "event: chat.message" in response.text
    assert '"role":"tool"' in response.text
    assert '"tool_result":{"callId":"call-1"' in response.text


def test_chat_stream_route_requires_known_chat() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    conversations = _conversations(service, broker, _provider("x"))
    client = TestClient(_make_app(service, broker, conversations))
    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/chat-999/events/stream", headers=HEADERS
    )
    assert response.status_code == 404
