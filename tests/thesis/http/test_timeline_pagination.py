"""Backward pagination contracts for complete, scoped chat history."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from unittest.mock import Mock

import pytest
from azure.cosmos import ContainerProxy
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thesis.http.timeline import create_timeline_router
from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline
from thesis_runtime.conversations import ChatService

WORKSPACE = "workspace-a"


@pytest.fixture
def history_size() -> int:
    return 31


@pytest.fixture()
def timeline_client(
    service: ChatService, history_size: int
) -> Iterator[tuple[TestClient, str]]:
    chat = service.create_chat(WORKSPACE, title="History", owner_actor_id="operator")
    # Cosmos is the external port here; generate its history from one current
    # message payload instead of exercising intake thousands of times.
    message = service.send_message(
        WORKSPACE, chat.chat_id, actor_id="operator", body="History"
    ).as_dict()
    documents: list[dict[str, object]] = [
        {
            "id": f"item-{seq}",
            "chatId": chat.chat_id,
            "threadId": "agent-a" if seq % 2 else "root",
            "kind": "message" if seq % 3 else "event",
            "workspaceId": WORKSPACE,
            "ts": "2026-01-01T00:00:00Z",
            "payload": (
                {**message, "messageId": f"message-{seq}", "body": f"Message {seq}"}
                if seq % 3
                else {
                    "actorId": "operator",
                    "actorScope": "user",
                    "correlationId": "corr",
                    "cursor": seq,
                    "eventId": f"evt-{seq}",
                    "eventType": "message.accepted",
                    "executionId": "execution",
                    "intentEpoch": 0,
                    "lateResult": False,
                    "occurredAt": "2026-01-01T00:00:00Z",
                    "payload": {},
                    "workspaceId": WORKSPACE,
                }
            ),
            "seq": seq,
            "_etag": "internal",
        }
        for seq in range(1, history_size + 1)
    ]
    for item in documents:
        if item.get("kind") == "message":
            # Stored message documents hoist ownership and record timestamps;
            # the transport projection must select only wire fields.
            item["createdAt"] = item.pop("ts")
            item["recordType"] = "ChatMessage"
            item["conversationId"] = item["threadId"]
            item["rootConversationId"] = chat.chat_id
            item["parentConversationId"] = None
    documents.extend(
        [
            {
                "chatId": "other-chat",
                "threadId": "agent-a",
                "kind": "message",
                "seq": 10_000,
            },
        ]
    )

    def query_items(
        *,
        query: str,
        parameters: list[dict[str, object]],
        partition_key: str,
        max_item_count: int,
    ) -> Iterator[dict[str, object]]:
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        assert partition_key == WORKSPACE
        items = [item for item in documents if item["chatId"] == values["@chat"]]
        if "@thread" in values and "c.threadId = @thread" in query:
            items = [
                item for item in items if item.get("threadId") == values["@thread"]
            ]
        if "@before" in values:
            items = [
                item
                for item in items
                if "seq" in item
                and cast(int, item["seq"]) < cast(int, values["@before"])
            ]
        if "@after" in values:
            items = [
                item
                for item in items
                if "seq" in item
                and cast(int, item["seq"]) > cast(int, values["@after"])
            ]
        items.sort(
            key=lambda item: cast(int, item.get("seq", 0)), reverse="DESC" in query
        )
        if "TOP @limit" in query:
            assert values["@limit"] == max_item_count
            items = items[:max_item_count]
        return iter(items)

    container = Mock(spec=ContainerProxy)
    container.read_item.return_value = {"nextSeq": history_size}
    container.query_items.side_effect = query_items
    app = FastAPI()
    app.state.chat_service = service
    app.state.timeline = CosmosTimeline(cast(ContainerProxy, container))
    app.include_router(create_timeline_router(max_poll_cycles=1))
    with TestClient(app) as client:
        yield client, f"/v1/workspaces/{WORKSPACE}/chats/{chat.chat_id}/timeline"


@pytest.mark.parametrize("history_size", [2_505])
def test_backward_pages_recover_complete_scoped_history(
    timeline_client: tuple[TestClient, str],
    history_size: int,
) -> None:
    client, url = timeline_client
    for thread in (None, "agent-a", "root"):
        params: dict[str, str | int] = {"limit": 1_000}
        if thread is not None:
            params["thread"] = thread
        recovered: list[int] = []
        for _ in range(50):
            response = client.get(url, params=params)
            assert response.status_code == 200
            page = response.json()
            sequences = [item["seq"] for item in page["items"]]
            assert sequences == sorted(sequences)
            assert len(sequences) <= 1_000
            for item in page["items"]:
                assert (
                    not {"_etag", "recordType", "conversationId", "createdAt"}
                    & item.keys()
                )
                assert isinstance(item["ts"], str) and item["ts"].endswith("Z")
                if thread is not None:
                    assert item["threadId"] == thread
            if not sequences:
                assert page["nextBefore"] is None
                break
            assert page["nextBefore"] == min(sequences)
            if recovered:
                assert max(sequences) < min(recovered)
            recovered = sequences + recovered
            params["before"] = page["nextBefore"]
        else:
            pytest.fail("Backward pagination did not terminate")
        expected = [
            seq
            for seq in range(1, history_size + 1)
            if thread is None or seq % 2 == (1 if thread == "agent-a" else 0)
        ]
        assert len(expected) > 1_000
        assert recovered == expected


@pytest.mark.parametrize("history_size", [0])
def test_invalid_pagination_requests_are_rejected_without_history(
    timeline_client: tuple[TestClient, str],
    history_size: int,
) -> None:
    client, url = timeline_client
    assert client.get(url).json()["after"] == history_size
    for after in (0, 5):
        response = client.get(url, params={"after": after, "before": 10})
        assert response.status_code == 422
        assert "mutually exclusive" in response.json()["detail"]
    for params in (
        {"before": -1},
        {"before": "invalid"},
        {"limit": 0},
        {"limit": 1_001},
    ):
        assert client.get(url, params=params).status_code == 422


def test_latest_forward_and_empty_pages(
    timeline_client: tuple[TestClient, str],
) -> None:
    client, url = timeline_client
    for params in ({"limit": 2}, {"after": 0, "limit": 2}):
        page = client.get(url, params=params).json()
        assert [item["seq"] for item in page["items"]] == [30, 31]
        assert page["nextBefore"] == 30
    page = client.get(url, params={"after": 5, "limit": 2}).json()
    assert [item["seq"] for item in page["items"]] == [6, 7]
    assert page["nextBefore"] == 6
    page = client.get(url, params={"before": 9, "limit": 2, "thread": "agent-a"}).json()
    assert [item["seq"] for item in page["items"]] == [5, 7]
    assert page["nextBefore"] == 5
    for params in ({"before": 0}, {"before": 1}, {"thread": "missing"}):
        assert client.get(url, params=params).json() == {
            "items": [],
            "nextBefore": None,
            "after": 31,
        }


def test_pagination_preserves_workspace_chat_validation(
    timeline_client: tuple[TestClient, str],
) -> None:
    client, url = timeline_client
    assert (
        client.get(
            url.replace(WORKSPACE, "other-workspace"), params={"before": 10}
        ).status_code
        == 404
    )


def test_sse_shape_and_resume_are_unchanged(
    timeline_client: tuple[TestClient, str],
) -> None:
    client, url = timeline_client
    response = client.get(
        f"{url}/stream", params={"after": 29, "limit": 2, "thread": "root"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("id: 30\nevent: timeline\ndata: ")
    assert response.text.count("event: timeline") == 1
    assert "nextBefore" not in response.text
    assert "_etag" not in response.text


def test_history_watermark_is_chat_wide_and_sse_only_reads_subsequent_records(
    timeline_client: tuple[TestClient, str],
) -> None:
    client, url = timeline_client
    snapshot = client.get(url, params={"thread": "root", "limit": 2}).json()
    assert snapshot["after"] == 31
    assert max(item["seq"] for item in snapshot["items"]) == 30
    stream = client.get(f"{url}/stream", params={"after": snapshot["after"]})
    assert stream.status_code == 200 and stream.text == ""
    assert (
        client.get(f"{url}/stream", params={"after": 29, "limit": 2}).text.count(
            "event: timeline"
        )
        == 2
    )
