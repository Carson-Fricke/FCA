"""Workflow definition routes expose registry topology without execution."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis.workflows.core.phase import ConversationPhase
from thesis.workflows.core.registry import WorkflowRegistration, WorkflowRegistry
from thesis.workflows.core.spec import WorkflowSpec
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.registry.hypothesis_development.primitives import (
    _WORKFLOW_VERSION,
)

TOKEN = "thesis-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("version", [None, "2.3.0"])
def test_definition_preserves_arbitrary_topology_without_executing(
    prefix: str, version: str | None
) -> None:
    spec: WorkflowSpec[Any, Any, Any] = WorkflowSpec(
        workflow_id="custom-research",
        version="2.3.0",
        title="Custom research",
        phases=(
            ConversationPhase(phase="draft"),
            ConversationPhase(phase="review"),
            ConversationPhase(phase="repair"),
            ConversationPhase(phase="publish", terminal=True),
        ),
        transitions={
            "draft": frozenset({"review", "draft"}),
            "review": frozenset({"publish", "repair"}),
            "repair": frozenset({"draft"}),
        },
    )
    execute = Mock(side_effect=AssertionError("must not execute"))
    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(execute=execute, workflow_factory=lambda: Workflow(spec))
    )
    client = TestClient(create_app(bearer_token=TOKEN, workflow_registry=registry))

    response = client.get(
        f"{prefix}/v1/workflows/custom-research",
        params={} if version is None else {"version": version},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.json() == {
        "workflow_id": "custom-research",
        "version": "2.3.0",
        "title": "Custom research",
        "phases": ["draft", "review", "repair", "publish"],
        "terminal_phases": ["publish"],
        "transitions": {
            "draft": ["draft", "review"],
            "review": ["publish", "repair"],
            "repair": ["draft"],
            "publish": [],
        },
    }
    execute.assert_not_called()


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("version", [None, "1.0.0"])
def test_definition_requires_authentication_and_unknown_id_returns_404(
    prefix: str,
    version: str | None,
) -> None:
    client = TestClient(create_app(bearer_token=TOKEN))
    path = f"{prefix}/v1/workflows/not-registered"

    assert client.get(path).status_code == 401
    response = client.get(
        path, params={} if version is None else {"version": version}, headers=HEADERS
    )
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "unknown_workflow",
            "message": "unknown workflow: 'not-registered'",
        }
    }


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("version", [None, _WORKFLOW_VERSION])
def test_builtin_definition_is_available_without_execution_services(
    prefix: str, version: str | None
) -> None:
    client = TestClient(create_app(bearer_token=TOKEN))

    response = client.get(
        f"{prefix}/v1/workflows/hypothesis-development",
        params={} if version is None else {"version": version},
        headers=HEADERS,
    )

    assert response.status_code == 200
    definition = response.json()
    assert definition["workflow_id"] == "hypothesis-development"
    assert definition["version"] == _WORKFLOW_VERSION
    verdict_targets = definition["transitions"]["verdict"]
    assert "model_creation" in verdict_targets
    assert "discovery" in verdict_targets
    assert "hypothesis_creation" not in verdict_targets
    assert definition["phases"][:2] == ["discovery", "model_creation"]
    assert definition["terminal_phases"] == ["completed", "failed"]
    assert definition["transitions"]["completed"] == []
    assert definition["transitions"]["failed"] == []


def test_breaking_builtin_topology_has_a_new_major_version() -> None:
    assert _WORKFLOW_VERSION == "2.3.0"


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("version", ["1.0.0", "99.0.0", ""])
def test_builtin_definition_rejects_unsupported_versions(
    prefix: str, version: str
) -> None:
    client = TestClient(create_app(bearer_token=TOKEN))

    response = client.get(
        f"{prefix}/v1/workflows/hypothesis-development",
        params={"version": version},
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "unsupported_workflow_version",
            "message": f"unsupported workflow version: {version!r}",
            "details": {
                "requested_version": version,
                "current_version": _WORKFLOW_VERSION,
            },
        }
    }
