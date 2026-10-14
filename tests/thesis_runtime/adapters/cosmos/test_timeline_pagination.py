"""Cosmos query contracts for bounded timeline pages and sequence batches."""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock

import pytest
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline, counter_id
from thesis_runtime.conversations.streaming import CommitNotifier


@pytest.mark.parametrize("thread", [None, "agent' OR true"])
def test_read_before_uses_parameterized_bounded_descending_query(
    thread: str | None,
) -> None:
    container = Mock(spec=ContainerProxy)
    container.query_items.return_value = iter(
        [{"seq": 9, "workspaceId": "workspace"}, {"seq": 7, "workspaceId": "workspace"}]
    )
    timeline = CosmosTimeline(cast(ContainerProxy, container))
    assert timeline.read_before(
        workspace_id="workspace",
        chat_id="chat' OR true",
        before_seq=10,
        limit=2,
        thread_id=thread,
    ) == [
        {"seq": 7, "workspaceId": "workspace"},
        {"seq": 9, "workspaceId": "workspace"},
    ]
    arguments = container.query_items.call_args.kwargs
    query = arguments["query"]
    assert query.startswith("SELECT TOP @limit * FROM c ")
    assert "c.chatId = @chat" in query
    assert "c.seq < @before" in query
    assert query.endswith("ORDER BY c.seq DESC")
    assert ("c.threadId = @thread" in query) == (thread is not None)
    assert "OR true" not in query
    expected = [
        {"name": "@limit", "value": 2},
        {"name": "@chat", "value": "chat' OR true"},
        {"name": "@before", "value": 10},
    ]
    if thread is not None:
        expected.append({"name": "@thread", "value": thread})
    assert arguments["parameters"] == expected
    assert arguments["partition_key"] == "workspace"
    assert arguments["max_item_count"] == 2


def test_read_before_returns_empty_page() -> None:
    container = Mock(spec=ContainerProxy)
    container.query_items.return_value = iter([])
    timeline = CosmosTimeline(cast(ContainerProxy, container))
    assert (
        timeline.read_before(
            workspace_id="workspace", chat_id="chat", before_seq=1, limit=200
        )
        == []
    )


def test_forward_limit_bounds_cosmos_query_not_only_client_slice() -> None:
    container = Mock(spec=ContainerProxy)
    container.query_items.return_value = iter(
        [{"seq": 3, "workspaceId": "workspace"}, {"seq": 4, "workspaceId": "workspace"}]
    )
    timeline = CosmosTimeline(cast(ContainerProxy, container))
    assert timeline.read_after(
        workspace_id="workspace", chat_id="chat", after_seq=2, limit=2
    ) == [
        {"seq": 3, "workspaceId": "workspace"},
        {"seq": 4, "workspaceId": "workspace"},
    ]
    arguments = container.query_items.call_args.kwargs
    assert arguments["query"].startswith("SELECT TOP @limit * FROM c ")
    assert {"name": "@limit", "value": 2} in arguments["parameters"]
    assert arguments["partition_key"] == "workspace"


def test_create_with_sequence_notifies_committed_change() -> None:
    counter = {
        "id": counter_id("chat"),
        "workspaceId": "workspace",
        "nextSeq": 7,
        "_etag": "1",
    }
    container = Mock(spec=ContainerProxy)
    container.read_item.side_effect = [
        CosmosResourceNotFoundError(status_code=404),
        counter,
        CosmosResourceNotFoundError(status_code=404),
    ]
    notifier = CommitNotifier()
    timeline = CosmosTimeline(cast(ContainerProxy, container), notifier=notifier)
    version = notifier.version("workspace", "chat")
    timeline.append(
        chat_id="chat",
        workspace_id="workspace",
        kind="event",
        payload={"value": 1},
        item_id="event:one",
    )
    assert notifier.version("workspace", "chat") == version + 1
    container.read_item.side_effect = [
        {
            "id": "event:one",
            "workspaceId": "workspace",
            "chatId": "chat",
            "seq": 8,
            "payload": {"value": 1},
        }
    ]
    timeline.append(
        chat_id="chat",
        workspace_id="workspace",
        kind="event",
        payload={"value": 2},
        item_id="event:one",
    )
    assert notifier.version("workspace", "chat") == version + 1
