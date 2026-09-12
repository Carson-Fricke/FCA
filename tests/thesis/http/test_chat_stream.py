"""HTTP tests for chat-scoped streamed replies."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from fastapi.testclient import TestClient

from thesis.chat import (
    CHAT_MESSAGE,
    ChatMessage,
    ChatReplyService,
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


def _make_app(
    service: ChatService, broker: InMemoryChatStreamBroker, reply: ChatReplyService
):
    return create_app(
        bearer_token=TOKEN,
        chat_service=service,
        chat_stream_broker=broker,
        chat_reply_service=reply,
        stream_sleeper=_no_sleeper,
        stream_max_poll_cycles=1,
    )


def test_post_message_schedules_a_streamed_reply() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    reply = ChatReplyService(
        chat_service=service, broker=broker, provider=_provider("Pong")
    )
    client = TestClient(_make_app(service, broker, reply))
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

    frames = broker.read(WORKSPACE, chat["chat_id"])
    assert any(frame.event == CHAT_MESSAGE for frame in frames)
    stored = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat['chat_id']}/messages", headers=HEADERS
    ).json()["items"]
    assert [message["role"] for message in stored] == ["user", "assistant"]


def test_chat_stream_route_emits_reply_frames() -> None:
    service = ChatService()
    broker = InMemoryChatStreamBroker()
    reply = ChatReplyService(
        chat_service=service, broker=broker, provider=_provider("Pong")
    )
    client = TestClient(_make_app(service, broker, reply))
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=HEADERS,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    service.create_message(WORKSPACE, chat["chat_id"], actor_id="operator", body="ping")
    reply.respond(WORKSPACE, chat["chat_id"])

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

    def provider(
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

    reply = ChatReplyService(chat_service=service, broker=broker, provider=provider)
    client = TestClient(_make_app(service, broker, reply))
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=HEADERS,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    service.create_message(WORKSPACE, chat["chat_id"], actor_id="operator", body="run")
    reply.respond(WORKSPACE, chat["chat_id"])

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
    client = TestClient(
        _make_app(
            service,
            broker,
            ChatReplyService(
                chat_service=service, broker=broker, provider=_provider("x")
            ),
        )
    )
    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/chat-999/events/stream", headers=HEADERS
    )
    assert response.status_code == 404
