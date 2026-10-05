"""The workspace gate route returns a summary without the snapshot detail."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from thesis.http.workspaces import WorkspaceDataService, create_workspaces_router


def test_workspace_gate_returns_summary_without_counts() -> None:
    service = WorkspaceDataService()
    service.create_workspace("Gate")
    app = FastAPI()
    app.state.workspace_service = service
    app.include_router(create_workspaces_router())
    client = TestClient(app)

    response = client.get("/v1/workspaces/gate")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "gate"
    assert body["name"] == "Gate"
    assert "counts" not in body
    assert "overview" not in body


def test_workspace_gate_reports_unknown_workspace() -> None:
    app = FastAPI()
    app.state.workspace_service = WorkspaceDataService()
    app.include_router(create_workspaces_router())
    client = TestClient(app)

    assert client.get("/v1/workspaces/missing").status_code == 404
