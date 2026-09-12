"""HTTP tests for the recursive sub-agent REST and event surface."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from thesis.chat import ChatService
from thesis.execution.events import ExecutionEvent, ExecutionEventType
from thesis.execution.sub_agents import SubAgentContextSpec, append_sub_agent_event

WORKSPACE = "workspace-a"
HEADERS = {"Authorization": "Bearer thesis-test-token"}
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _seed(
    service: ChatService,
    execution: Any,
    sub_agent_id: str,
    parent: str | None,
    *,
    event_type: ExecutionEventType = ExecutionEventType.SUB_AGENT_SPAWNED,
    accepts_input: bool = False,
    extra: dict[str, object] | None = None,
    phase: str = "ideation",
    role: str = "orchestrator",
) -> ExecutionEvent:
    return append_sub_agent_event(
        service.event_store,
        execution_id=execution.execution_id,
        workspace_id=WORKSPACE,
        event_type=event_type,
        occurred_at=NOW,
        sub_agent_id=sub_agent_id,
        parent_sub_agent_id=parent,
        phase=phase,
        role=role,
        context=SubAgentContextSpec(),
        accepts_input=accepts_input,
        extra=extra,
    )


def _execution(service: ChatService, launch: Any) -> Any:
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    execution = launch(chat.chat_id)
    return chat, execution


def test_recursive_listing_supports_depth_three(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    _, execution = _execution(service, launch)
    base = f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}"
    for sub_agent_id, parent in (
        ("agent-a", None),
        ("agent-b", "agent-a"),
        ("agent-c", "agent-b"),
        ("agent-d", "agent-c"),
        ("agent-e", "agent-a"),
    ):
        _seed(service, execution, sub_agent_id, parent)

    roots = client.get(base + "/sub_agents", headers=HEADERS).json()["items"]
    assert [node["sub_agent_id"] for node in roots] == ["agent-a"]

    record = client.get(base + "/sub_agents/agent-a", headers=HEADERS).json()
    assert {child["sub_agent_id"] for child in record["children"]} == {
        "agent-b",
        "agent-e",
    }

    children = client.get(
        base + "/sub_agents/agent-a/agent-b/sub_agents", headers=HEADERS
    ).json()["items"]
    assert [node["sub_agent_id"] for node in children] == ["agent-c"]

    deep = client.get(
        base + "/sub_agents/agent-a/agent-b/agent-c/agent-d", headers=HEADERS
    )
    assert deep.status_code == 200
    assert deep.json()["sub_agent_id"] == "agent-d"
    assert deep.json()["parent_sub_agent_id"] == "agent-c"

    assert client.get(base + "/sub_agents/agent-c", headers=HEADERS).status_code == 404
    assert (
        client.get(base + "/sub_agents/agent-a/agent-d", headers=HEADERS).status_code
        == 404
    )


def test_own_events_versus_descendants_preserve_the_global_cursor(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    _, execution = _execution(service, launch)
    base = f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}"
    _seed(service, execution, "agent-a", None)
    second = _seed(service, execution, "agent-b", "agent-a")
    _seed(service, execution, "agent-c", "agent-b")
    _seed(
        service,
        execution,
        "agent-c",
        "agent-b",
        event_type=ExecutionEventType.SUB_AGENT_PROGRESS,
        extra={"kind": "tool", "toolCall": {"name": "backtest"}},
    )

    own = client.get(
        base + "/sub_agents/agent-a/agent-b/events", headers=HEADERS
    ).json()
    assert [event["payload"]["subAgentId"] for event in own["events"]] == ["agent-b"]
    assert own["events"][0]["cursor"] == second.cursor

    descendants = client.get(
        base + "/sub_agents/agent-a/agent-b/events?include_descendants=true",
        headers=HEADERS,
    ).json()
    assert {event["payload"]["subAgentId"] for event in descendants["events"]} == {
        "agent-b",
        "agent-c",
    }
    cursors = [event["cursor"] for event in descendants["events"]]
    assert cursors == sorted(cursors)
    assert cursors[0] == second.cursor

    resumed = client.get(
        base
        + "/sub_agents/agent-a/agent-b/events"
        + f"?include_descendants=true&after_cursor={second.cursor}",
        headers=HEADERS,
    ).json()
    assert resumed["afterCursor"] == second.cursor
    assert all(event["cursor"] > second.cursor for event in resumed["events"])
    assert resumed["nextCursor"] == descendants["nextCursor"]

    exhausted = client.get(
        base
        + "/sub_agents/agent-a/agent-b/events"
        + f"?include_descendants=true&after_cursor={descendants['nextCursor']}",
        headers=HEADERS,
    ).json()
    assert exhausted["events"] == []


def test_post_message_is_accepted_only_when_accepting_input(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    chat, execution = _execution(service, launch)
    base = f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}"
    _seed(service, execution, "agent-a", None)
    _seed(service, execution, "agent-b", "agent-a")
    _seed(
        service,
        execution,
        "agent-b",
        "agent-a",
        event_type=ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
        accepts_input=True,
        extra={"question": "provide the data pull window"},
    )
    _seed(service, execution, "agent-f", "agent-a")
    _seed(
        service,
        execution,
        "agent-f",
        "agent-a",
        event_type=ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
        accepts_input=True,
        extra={"question": "which symbols?"},
    )

    invalid = client.post(
        base + "/sub_agents/agent-a/agent-f/messages",
        headers=HEADERS,
        json={"actor_id": "operator", "body": "bad\u0007control"},
    )
    assert invalid.status_code == 422

    accepted = client.post(
        base + "/sub_agents/agent-a/agent-b/messages",
        headers=HEADERS,
        json={"actor_id": "operator", "body": "Use a 60-day window."},
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["subAgentId"] == "agent-b"
    assert body["kind"] == "sub_agent"
    assert body["data"]["subAgentId"] == "agent-b"
    assert body["data"]["phase"] == "ideation"

    again = client.post(
        base + "/sub_agents/agent-a/agent-b/messages",
        headers=HEADERS,
        json={"actor_id": "operator", "body": "Another."},
    )
    assert again.status_code == 409

    root = client.post(
        base + "/sub_agents/agent-a/messages",
        headers=HEADERS,
        json={"actor_id": "operator", "body": "Not accepting."},
    )
    assert root.status_code == 409

    missing = client.post(
        base + "/sub_agents/agent-a/agent-z/messages",
        headers=HEADERS,
        json={"actor_id": "operator", "body": "?"},
    )
    assert missing.status_code == 404

    transcript = client.get(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat.chat_id}/messages",
        headers=HEADERS,
    ).json()["items"]
    stored = next(
        item for item in transcript if item["message_id"] == body["message_id"]
    )
    assert stored["kind"] == "sub_agent"
    assert stored["data"]["subAgentId"] == "agent-b"
    assert stored["execution_id"] == execution.execution_id


def test_post_message_keeps_on_demand_input_enabled(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    _, execution = _execution(service, launch)
    base = f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}"
    _seed(service, execution, "ideation", None, accepts_input=True)

    first = client.post(
        base + "/sub_agents/ideation/messages",
        headers=HEADERS,
        json={"actor_id": "operator", "body": "Focus on liquid names."},
    )
    assert first.status_code == 200

    second = client.post(
        base + "/sub_agents/ideation/messages",
        headers=HEADERS,
        json={"actor_id": "operator", "body": "Cap the horizon at 5 days."},
    )
    assert second.status_code == 200

    record = client.get(base + "/sub_agents/ideation", headers=HEADERS).json()
    assert record["accepts_input"] is True


def test_node_event_stream_emits_only_the_node_scope(
    client: TestClient, service: ChatService, launch: Any
) -> None:
    _, execution = _execution(service, launch)
    base = f"/v1/workspaces/{WORKSPACE}/executions/{execution.execution_id}"
    _seed(service, execution, "agent-a", None)
    _seed(service, execution, "agent-b", "agent-a")
    _seed(
        service,
        execution,
        "agent-b",
        "agent-a",
        event_type=ExecutionEventType.SUB_AGENT_PROGRESS,
        extra={"kind": "thinking", "text": "weighing evidence"},
    )
    _seed(service, execution, "agent-c", "agent-a")

    response = client.get(
        base + "/sub_agents/agent-a/agent-b/events/stream", headers=HEADERS
    )

    assert response.status_code == 200
    assert response.text.count("event: sub_agent.") == 2
    assert '"subAgentId":"agent-b"' in response.text
    assert '"subAgentId":"agent-c"' not in response.text
