"""Root and prefixed routes share injected state and error contracts."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from thesis.agent.runtime.conversation import Conversations
from thesis.chat import ChatService
from thesis.http.application import create_app
from thesis.http.chat import create_chat_router
from thesis.http.workspaces import build_local_workspace_data
from thesis.workflows.core.registry import WorkflowRegistry

HEADERS = {"Authorization": "Bearer test-token"}


def test_injected_container_is_shared_by_both_surfaces() -> None:
    chat = ChatService()
    conversations = Conversations(chat=chat)
    workspace = build_local_workspace_data(chat_service=chat)
    app = create_app(
        bearer_token="test-token",
        conversations=conversations,
        workspace_data=workspace,
        workflow_registry=WorkflowRegistry(),
    )
    client = TestClient(app)
    created = client.post(
        "/api/v1/workspaces", headers=HEADERS, json={"name": "Shared"}
    ).json()
    workspace_id = created["id"]
    path = f"/v1/workspaces/{workspace_id}/chats"
    thread = client.post(
        path, headers=HEADERS, json={"title": "Shared", "owner_actor_id": "operator"}
    ).json()
    chat_id = thread["chat_id"]
    assert client.get(f"/api{path}", headers=HEADERS).json()["items"] == [thread]
    assert app.state.chat_service is chat
    assert app.state.conversations is conversations
    assert app.state.workspace_service is workspace.workspaces
    assert client.get("/v1/workspaces", headers=HEADERS).json()["items"] == [created]
    message = conversations.root(workspace_id, chat_id).send(
        body="Shared durable message", actor_id="operator"
    )
    for prefix in ("", "/api"):
        response = client.get(f"{prefix}{path}/{chat_id}/messages", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["items"] == [message.as_dict()]


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_missing_services_fail_closed_without_mutating_state(prefix: str) -> None:
    app = create_app(
        bearer_token="test-token", in_memory=False, workflow_registry=WorkflowRegistry()
    )
    client = TestClient(app)
    for path, code in (
        ("/v1/workspaces", "workspace_service_unavailable"),
        ("/v1/workspaces/w/chats", "chat_service_unavailable"),
        ("/v1/workspaces/w/screeners", "screener_service_unavailable"),
    ):
        response = client.get(prefix + path, headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == code
    assert not hasattr(app.state, "chat_service")
    assert client.get(prefix + "/healthz").status_code == 200
    assert client.get(prefix + "/v1/auth/check").status_code == 401


def test_missing_chat_never_creates_a_thread_through_conversations() -> None:
    chat = ChatService()
    conversations = Conversations(chat=chat)
    app = create_app(
        bearer_token="test-token",
        conversations=conversations,
        workflow_registry=WorkflowRegistry(),
    )
    client = TestClient(app)
    for prefix in ("", "/api"):
        response = client.post(
            prefix + "/v1/workspaces/w/chats/missing/messages",
            headers=HEADERS,
            json={"actor_id": "operator", "body": "hello"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "record_not_found"
    assert chat.list_chats("w") == ()


def test_router_alone_never_manufactures_chat_service() -> None:
    app = FastAPI()
    app.include_router(create_chat_router())
    assert TestClient(app).get("/v1/workspaces/w/chats").status_code == 503
    assert not hasattr(app.state, "chat_service")


def test_openapi_exports_generated_request_and_error_contracts() -> None:
    app = create_app(in_memory=False, workflow_registry=WorkflowRegistry())
    schema = app.openapi()
    models = schema["components"]["schemas"]
    assert "CreateMessageRequest" in models
    assert "ErrorResponse" in models
    for prefix in ("", "/api"):
        route = schema["paths"][
            prefix + "/v1/workspaces/{workspace_id}/chats/{chat_id}/messages"
        ]["post"]
        assert route["responses"]["422"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def test_errors_preserve_details_and_hide_unknown_causes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(bearer_token="test-token", workflow_registry=WorkflowRegistry())

    async def structured() -> None:
        raise HTTPException(
            409, {"code": "conflict", "message": "rejected", "details": {"position": 7}}
        )

    async def unknown() -> None:
        raise RuntimeError("secret-api-key")

    for prefix in ("", "/api"):
        app.add_api_route(prefix + "/structured", structured)
        app.add_api_route(prefix + "/unknown", unknown)
    client = TestClient(app, raise_server_exceptions=False)
    for prefix in ("", "/api"):
        assert client.get(prefix + "/structured").json() == {
            "error": {
                "code": "conflict",
                "message": "rejected",
                "details": {"position": 7},
            }
        }
        response = client.get(prefix + "/unknown")
        assert response.status_code == 500
        assert response.json() == {
            "error": {"code": "internal_error", "message": "request failed"}
        }
        invalid = client.post(
            prefix + "/v1/workspaces", headers=HEADERS, json={"name": ""}
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["details"][0]["input"] == ""
    assert "secret-api-key" in caplog.text


def test_snapshot_only_reports_screeners_with_real_count_source() -> None:
    from thesis.http.workspaces import WorkspaceDataService
    from thesis.screening.definitions import ScreenerDefinition

    bundle = build_local_workspace_data()
    workspace_id = str(bundle.workspaces.create_workspace("Counts")["id"])
    counts = bundle.workspaces.snapshot(workspace_id)["counts"]
    assert isinstance(counts, dict)
    assert counts["screeners"] == 0
    assert bundle.screeners is not None
    bundle.screeners.save_definition(
        workspace_id,
        ScreenerDefinition(
            name="Counted",
            lookback_days=10,
            bar_intervals=("1d",),
            rank_metric="score",
            source="source",
        ),
    )
    counts = bundle.workspaces.snapshot(workspace_id)["counts"]
    assert isinstance(counts, dict)
    assert counts["screeners"] == 1
    standalone = WorkspaceDataService()
    standalone.create_workspace("Counts")
    counts = standalone.snapshot("counts")["counts"]
    assert isinstance(counts, dict)
    assert "screeners" not in counts
