"""Workspace state retains only lineage, evidence, and consent audit records."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thesis.http.state import (
    InMemoryWorkspaceStateStore,
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


def test_state_round_trip_preserves_supported_records(
    document: dict[str, object],
) -> None:
    app = FastAPI()
    app.state.workspace_state_service = WorkspaceStateService()
    app.include_router(create_workspace_state_router())
    with TestClient(app) as client:
        response = client.put("/v1/workspaces/alpha/state", json=document)
        assert response.status_code == 200
        assert response.json() == document
        assert client.get("/v1/workspaces/alpha/state").json() == document


def test_state_discards_unsupported_fields_on_read_and_write(
    document: dict[str, object],
) -> None:
    store = InMemoryWorkspaceStateStore()
    service = WorkspaceStateService(store)
    legacy = {**document, "retiredPlanning": {"tasks": []}}
    store.save("alpha", legacy)
    assert service.state("alpha") == document
    service.save_state("alpha", legacy)
    assert store.load("alpha") == document


@pytest.mark.parametrize("missing", ["lineages", "evidence", "consentAudit"])
def test_state_requires_retained_records(
    document: dict[str, object], missing: str
) -> None:
    del document[missing]
    service = WorkspaceStateService()
    with pytest.raises(ValueError, match=missing):
        service.save_state("alpha", document)
    assert service.store.load("alpha") is None


def test_state_missing_workspace_is_not_fabricated() -> None:
    with pytest.raises(KeyError, match="alpha"):
        WorkspaceStateService().state("alpha")
