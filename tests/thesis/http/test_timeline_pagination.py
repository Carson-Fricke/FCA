"""Backward pagination contracts for complete, scoped chat history."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from unittest.mock import Mock

import pytest
from azure.cosmos import ContainerProxy
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thesis.chat import ChatService
from thesis.cosmos.timeline import CosmosTimeline
from thesis.http.timeline import create_timeline_router

WORKSPACE = "workspace-a"


@pytest.fixture()
def timeline_client(service: ChatService) -> Iterator[tuple[TestClient, str]]:
    chat = service.create_chat(WORKSPACE, title="History", owner_actor_id="operator")
    documents: list[dict[str, object]] = [
        {
            "id": f"item-{seq}",
            "chatId": chat.chat_id,
            "threadId": "agent-a" if seq % 2 else "root",
            "kind": "message" if seq % 3 else "event",
            "workspaceId": WORKSPACE,
            "ts": "2026-01-01T00:00:00Z",
            "payload": (
                service.create_message(
                    WORKSPACE, chat.chat_id, actor_id="operator", body=f"Message {seq}"
                ).as_dict()
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
        for seq in range(1, 2_506)
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
    container.read_item.return_value = {"nextSeq": 2_505}
    container.query_items.side_effect = query_items
    app = FastAPI()
    app.state.chat_service = service
    app.state.timeline = CosmosTimeline(cast(ContainerProxy, container))
    app.include_router(create_timeline_router(max_poll_cycles=1))
    with TestClient(app) as client:
        yield client, f"/v1/workspaces/{WORKSPACE}/chats/{chat.chat_id}/timeline"


def test_stored_document_fields_never_leak_onto_the_wire(
    timeline_client: tuple[TestClient, str],
) -> None:
    client, url = timeline_client
    page = client.get(url, params={"limit": 3, "thread": "root"}).json()
    assert page["items"]
    for item in page["items"]:
        assert "recordType" not in item
        assert "conversationId" not in item
        assert "createdAt" not in item
        assert isinstance(item["ts"], str) and item["ts"].endswith("Z")


@pytest.mark.parametrize("thread", [None, "agent-a", "root"])
def test_backward_pages_recover_complete_scoped_history(
    timeline_client: tuple[TestClient, str], thread: str | None
) -> None:
    client, url = timeline_client
    params: dict[str, str | int] = {"limit": 1_000}
    if thread is not None:
        params["thread"] = thread
    recovered: list[int] = []
    for _ in range(10):
        response = client.get(url, params=params)
        assert response.status_code == 200
        page = response.json()
        sequences = [item["seq"] for item in page["items"]]
        assert sequences == sorted(sequences)
        assert len(sequences) <= 1_000
        assert all("_etag" not in item for item in page["items"])
        if thread is not None:
            assert all(item["threadId"] == thread for item in page["items"])
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
        for seq in range(1, 2_506)
        if thread is None or (seq % 2 == (1 if thread == "agent-a" else 0))
    ]
    assert len(expected) > 1_000
    assert recovered == expected


@pytest.mark.parametrize("after", [0, 5])
def test_before_and_explicit_after_are_mutually_exclusive(
    timeline_client: tuple[TestClient, str], after: int
) -> None:
    client, url = timeline_client
    response = client.get(url, params={"after": after, "before": 10})
    assert response.status_code == 422
    assert "mutually exclusive" in response.json()["detail"]


@pytest.mark.parametrize(
    "params",
    [{"before": -1}, {"before": "invalid"}, {"limit": 0}, {"limit": 1_001}],
)
def test_invalid_pagination_parameters_are_rejected(
    timeline_client: tuple[TestClient, str], params: dict[str, str | int]
) -> None:
    client, url = timeline_client
    assert client.get(url, params=params).status_code == 422


def test_latest_forward_and_empty_pages(
    timeline_client: tuple[TestClient, str],
) -> None:
    client, url = timeline_client
    for params in ({"limit": 2}, {"after": 0, "limit": 2}):
        page = client.get(url, params=params).json()
        assert [item["seq"] for item in page["items"]] == [2_504, 2_505]
        assert page["nextBefore"] == 2_504
    page = client.get(url, params={"after": 5, "limit": 2}).json()
    assert [item["seq"] for item in page["items"]] == [6, 7]
    assert page["nextBefore"] == 6
    for params in ({"before": 0}, {"before": 1}, {"thread": "missing"}):
        assert client.get(url, params=params).json() == {
            "items": [],
            "nextBefore": None,
            "after": 2_505,
        }


def test_backward_cursor_selects_nearest_older_scoped_items(
    timeline_client: tuple[TestClient, str],
) -> None:
    client, url = timeline_client
    page = client.get(url, params={"before": 9, "limit": 2, "thread": "agent-a"}).json()
    assert [item["seq"] for item in page["items"]] == [5, 7]
    assert page["nextBefore"] == 5


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
        f"{url}/stream", params={"after": 2_503, "limit": 2, "thread": "root"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("id: 2504\nevent: timeline\ndata: ")
    assert response.text.count("event: timeline") == 1
    assert "nextBefore" not in response.text
    assert "_etag" not in response.text


def test_history_watermark_is_chat_wide_and_sse_only_reads_subsequent_records(
    timeline_client: tuple[TestClient, str],
) -> None:
    client, url = timeline_client
    snapshot = client.get(url, params={"thread": "root", "limit": 2}).json()
    assert snapshot["after"] == 2_505
    assert max(item["seq"] for item in snapshot["items"]) == 2_504
    stream = client.get(f"{url}/stream", params={"after": snapshot["after"]})
    assert stream.status_code == 200 and stream.text == ""
    assert (
        client.get(f"{url}/stream", params={"after": 2_503, "limit": 2}).text.count(
            "event: timeline"
        )
        == 2
    )


@pytest.mark.parametrize("stream", [False, True])
def test_timeline_reads_do_not_project_or_repair(
    timeline_client: tuple[TestClient, str], stream: bool
) -> None:
    client, url = timeline_client
    response = client.get(url + ("/stream" if stream else ""), params={"after": 2_503})
    assert response.status_code == 200
