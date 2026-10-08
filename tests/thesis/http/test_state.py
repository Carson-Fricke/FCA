"""Workspace state retains only lineage, evidence, and consent audit records."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thesis.http.state import (
    WorkspaceStateService,
    create_workspace_state_router,
)


@pytest.fixture
def document() -> dict[str, object]:
    return {
        "lineages": [{"strategyId": "momentum", "revision": "abc123"}],
        "evidence": [{"id": "trial", "source": "backtest"}],
        "consentAudit": [{"id": "approval", "actor": "operator"}],
    }


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.state.workspace_state_service = WorkspaceStateService()
    app.include_router(create_workspace_state_router())
    return TestClient(app)


def test_state_round_trip_preserves_supported_records(
    client: TestClient, document: dict[str, object]
) -> None:
    response = client.put("/v1/workspaces/alpha/state", json=document)
    assert response.status_code == 200
    assert response.json() == document
    assert client.get("/v1/workspaces/alpha/state").json() == document


def test_state_discards_unsupported_fields_on_read_and_write(
    client: TestClient, document: dict[str, object]
) -> None:
    legacy = {**document, "retiredPlanning": {"tasks": []}}
    response = client.put("/v1/workspaces/alpha/state", json=legacy)
    assert response.status_code == 200
    assert response.json() == document
    assert client.get("/v1/workspaces/alpha/state").json() == document


@pytest.mark.parametrize("missing", ["lineages", "evidence", "consentAudit"])
def test_state_requires_retained_records(
    client: TestClient, document: dict[str, object], missing: str
) -> None:
    del document[missing]
    response = client.put("/v1/workspaces/alpha/state", json=document)
    assert response.status_code == 422
    assert missing in response.json()["detail"]["message"]
    assert client.get("/v1/workspaces/alpha/state").status_code == 404


def test_state_missing_workspace_is_not_fabricated(client: TestClient) -> None:
    response = client.get("/v1/workspaces/alpha/state")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "workspace_state_not_found"
