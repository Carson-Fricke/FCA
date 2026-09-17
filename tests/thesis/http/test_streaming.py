"""HTTP tests for the SSE execution event stream."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from thesis.chat import ChatService
from thesis.http.streaming import (
    CHAT_MESSAGE_EVENT_KIND,
    EPHEMERAL_EVENT_KINDS,
    PROVIDER_DELTA_EVENT_KIND,
)

WORKSPACE = "workspace-a"
TOKEN = "thesis-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _stream_url(execution_id: str, query: str = "") -> str:
    return f"/v1/workspaces/{WORKSPACE}/executions/{execution_id}/events/stream{query}"


def _read_stream(client: TestClient, url: str, headers: dict[str, str] = AUTH) -> str:
    with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return "".join(response.iter_text())


def _parse_frames(text: str) -> list[dict[str, str]]:
    frames: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            if current:
                frames.append(current)
                current = {}
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(": ")
        current[field] = value
    if current:
        frames.append(current)
    return frames


def _machine_frames(frames: list[dict[str, str]]) -> list[dict[str, str]]:
    ephemeral = {kind for kind in EPHEMERAL_EVENT_KINDS}
    return [frame for frame in frames if frame["event"] not in ephemeral]


def test_stream_fans_out_machine_events_and_chat_messages(
    client: TestClient, service: ChatService, launch: Callable[[str], Any]
) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=AUTH,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])
    service.start_workflow(WORKSPACE, execution.execution_id, actor_id="worker")
    service.steer_workflow(
        WORKSPACE,
        execution.execution_id,
        actor_id="operator",
        body="Use lower fees.",
    )

    frames = _parse_frames(_read_stream(client, _stream_url(execution.execution_id)))

    machine = _machine_frames(frames)
    assert [frame["event"] for frame in machine] == [
        "message.accepted",
        "workflow.launched",
        "workflow.started",
        "workflow.steered",
    ]
    assert [frame["id"] for frame in machine] == ["1", "2", "3", "4"]
    first = json.loads(machine[0]["data"])
    assert first["kind"] == "execution.event"
    assert first["eventType"] == "message.accepted"
    assert first["executionId"] == execution.execution_id

    chat_frames = [
        frame for frame in frames if frame["event"] == CHAT_MESSAGE_EVENT_KIND
    ]
    assert chat_frames
    assert all("id" not in frame for frame in chat_frames)
    messages = [json.loads(frame["data"])["message"] for frame in chat_frames]
    assert {item["message_id"] for item in messages} == {
        json.loads(machine[0]["data"])["payload"]["messageId"],
        json.loads(machine[-1]["data"])["payload"]["messageId"],
    }


def test_stream_resumes_from_last_event_id(
    client: TestClient, service: ChatService, launch: Callable[[str], Any]
) -> None:
    chat = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=AUTH,
        json={"title": "Research", "owner_actor_id": "operator"},
    ).json()
    execution = launch(chat["chat_id"])
    url = _stream_url(execution.execution_id)

    initial = _parse_frames(_read_stream(client, url))
    assert [frame["id"] for frame in _machine_frames(initial)] == ["1", "2", "3"]

    service.pause_workflow(WORKSPACE, execution.execution_id, actor_id="worker")

    resumed = _parse_frames(
        _read_stream(client, url, headers={**AUTH, "Last-Event-ID": "3"})
    )
    assert [frame["id"] for frame in _machine_frames(resumed)] == ["4"]

    queried = _parse_frames(_read_stream(client, f"{url}?after_cursor=3"))
    assert [frame["id"] for frame in _machine_frames(queried)] == ["4"]


def test_stream_rejects_invalid_last_event_id(
    client: TestClient, execution: Any
) -> None:
    rejected = client.get(
        _stream_url(execution.execution_id),
        headers={**AUTH, "Last-Event-ID": "not-a-cursor"},
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "invalid_last_event_id"

    negative = client.get(
        _stream_url(execution.execution_id),
        headers={**AUTH, "Last-Event-ID": "-1"},
    )
    assert negative.status_code == 400


def test_stream_provider_deltas_are_ephemeral_and_optional(
    make_client: Callable[..., TestClient],
    service: ChatService,
    execution: Any,
) -> None:
    deltas = ({"token": "hello"}, {"token": " world"})

    def delta_source(
        workspace_id: str, execution_id: str
    ) -> tuple[dict[str, str], ...]:
        assert workspace_id == WORKSPACE
        return deltas

    client = make_client(service, provider_delta_source=delta_source)
    frames = _parse_frames(_read_stream(client, _stream_url(execution.execution_id)))
    delta_frames = [
        frame for frame in frames if frame["event"] == PROVIDER_DELTA_EVENT_KIND
    ]
    assert len(delta_frames) == 2
    assert all("id" not in frame for frame in delta_frames)
    assert [json.loads(frame["data"])["delta"] for frame in delta_frames] == list(
        deltas
    )
    assert all(
        json.loads(frame["data"])["kind"] == PROVIDER_DELTA_EVENT_KIND
        for frame in delta_frames
    )
    assert all(
        json.loads(frame["data"])["executionId"] == execution.execution_id
        for frame in delta_frames
    )

    disabled = _parse_frames(
        _read_stream(
            client,
            _stream_url(execution.execution_id, "?provider_deltas=false"),
        )
    )
    assert not [
        frame for frame in disabled if frame["event"] == PROVIDER_DELTA_EVENT_KIND
    ]

    plain = make_client(service)
    frames = _parse_frames(_read_stream(plain, _stream_url(execution.execution_id)))
    assert not [
        frame for frame in frames if frame["event"] == PROVIDER_DELTA_EVENT_KIND
    ]


def test_stream_requires_bearer_token(client: TestClient, execution: Any) -> None:
    response = client.get(_stream_url(execution.execution_id))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_stream_unknown_execution_returns_404(client: TestClient) -> None:
    response = client.get(
        _stream_url("execution-999"), headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 404
