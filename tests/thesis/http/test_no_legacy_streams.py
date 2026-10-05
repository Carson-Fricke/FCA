"""Only the unified timeline exposes live conversation activity."""

import pytest
from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis.workflows.core.registry import WorkflowRegistry


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_unified_timeline_is_the_only_registered_stream(prefix: str) -> None:
    app = create_app(in_memory=False, workflow_registry=WorkflowRegistry())
    paths = app.openapi()["paths"]
    base = prefix + "/v1/workspaces/{workspace_id}"
    assert {
        path for path in paths if path.startswith(base) and path.endswith("/stream")
    } == {base + "/chats/{chat_id}/timeline/stream"}
    for suffix in (
        "/chats/{chat_id}/messages",
        "/chats/{chat_id}/timeline",
        "/executions/{execution_id}/events",
        "/chats/{chat_id}/sub_agents/{sub_agent_id}/events",
    ):
        assert "get" in paths[base + suffix]


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize(
    "suffix",
    [
        "/chats/chat/messages/stream",
        "/chats/chat/events/stream",
        "/executions/execution/events/stream",
        "/chats/chat/sub_agents/agent/events/stream",
    ],
)
def test_legacy_stream_paths_are_not_found(prefix: str, suffix: str) -> None:
    app = create_app(
        bearer_token="test-token",
        in_memory=False,
        workflow_registry=WorkflowRegistry(),
    )
    response = TestClient(app).get(
        prefix + "/v1/workspaces/workspace" + suffix,
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
