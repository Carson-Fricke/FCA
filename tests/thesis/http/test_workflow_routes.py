"""Workflow routes expose current topology, authentication and version checks."""

from typing import Any
from unittest.mock import Mock

from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis_runtime.workflows.phase import (
    ConversationPhase,
    DeterministicPhase,
    SplitPhase,
    TerminalPhase,
)
from thesis_runtime.workflows.registry import WorkflowRegistration, WorkflowRegistry
from thesis_runtime.workflows.spec import WorkflowSpec
from thesis_runtime.workflows.workflow import Workflow

TOKEN = "thesis-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_definition_preserves_topology_and_prefix_parity_without_executing() -> None:
    spec: WorkflowSpec[Any, Any, Any] = WorkflowSpec(
        workflow_id="custom-research",
        version="3.0.0",
        title="Custom research",
        phases=(
            ConversationPhase(phase="draft"),
            ConversationPhase(phase="review"),
            ConversationPhase(phase="repair"),
            TerminalPhase(phase="publish"),
        ),
        transitions={
            "draft": frozenset({"review", "draft"}),
            "review": frozenset({"publish", "repair"}),
            "repair": frozenset({"draft"}),
            "publish": frozenset(),
        },
    )
    execute = Mock(side_effect=AssertionError("must not execute"))
    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(execute=execute, workflow_factory=lambda: Workflow(spec))
    )
    with TestClient(
        create_app(bearer_token=TOKEN, workflow_registry=registry)
    ) as client:
        for prefix, params in (("", {}), ("/api", {"version": "3.0.0"})):
            response = client.get(
                f"{prefix}/v1/workflows/custom-research", params=params, headers=HEADERS
            )
            assert response.status_code == 200
            assert response.json() == {
                "workflow_id": "custom-research",
                "version": "3.0.0",
                "title": "Custom research",
                "phases": ["draft", "review", "repair", "publish"],
                "terminal_phases": ["publish"],
                "splits": {},
                "transitions": {
                    "draft": ["draft", "review"],
                    "review": ["publish", "repair"],
                    "repair": ["draft"],
                    "publish": [],
                },
            }
    execute.assert_not_called()


def test_definition_exposes_fixed_split_membership_and_its_deterministic_join() -> None:
    spec: WorkflowSpec[Any, Any, Any] = WorkflowSpec(
        workflow_id="parallel-review",
        version="1.0.0",
        title="Parallel review",
        phases=(
            SplitPhase(
                phase="split", branches={"left": "left", "right": "right"}, join="join"
            ),
            ConversationPhase(phase="left"),
            ConversationPhase(phase="right"),
            DeterministicPhase(phase="join", contract="combine", on_complete="done"),
            TerminalPhase(phase="done"),
        ),
        transitions={
            "split": frozenset({"left", "right"}),
            "left": frozenset({"join"}),
            "right": frozenset({"join"}),
            "join": frozenset({"done"}),
            "done": frozenset(),
        },
    )
    execute = Mock()
    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(execute=execute, workflow_factory=lambda: Workflow(spec))
    )
    with TestClient(
        create_app(bearer_token=TOKEN, workflow_registry=registry)
    ) as client:
        response = client.get("/v1/workflows/parallel-review", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["splits"] == {
            "split": {"branches": {"left": "left", "right": "right"}, "join": "join"}
        }
    execute.assert_not_called()


def test_definition_requires_authentication_and_unknown_id_returns_404() -> None:
    with TestClient(create_app(bearer_token=TOKEN)) as client:
        path = "/v1/workflows/not-registered"
        assert client.get(path).status_code == 401
        response = client.get(path, headers=HEADERS)
        assert response.status_code == 404
        assert response.json() == {
            "error": {
                "code": "unknown_workflow",
                "message": "unknown workflow: 'not-registered'",
            }
        }


def test_builtin_definition_and_current_version_checks_without_execution_services() -> (
    None
):
    with TestClient(create_app(bearer_token=TOKEN)) as client:
        path = "/v1/workflows/hypothesis-development"
        response = client.get(path, headers=HEADERS)
        assert response.status_code == 200
        definition = response.json()
        assert definition["workflow_id"] == "hypothesis-development"
        assert definition["splits"] == {}
        current_version = definition["version"]
        assert (
            client.get(
                path, params={"version": current_version}, headers=HEADERS
            ).json()
            == definition
        )
        assert definition["phases"][:2] == ["discovery", "model_creation"]
        assert {"model_creation", "discovery"} <= set(
            definition["transitions"]["verdict"]
        )
        assert definition["terminal_phases"] == ["completed", "failed"]
        assert definition["transitions"]["completed"] == []
        assert definition["transitions"]["failed"] == []
        for version in ("not-current", ""):
            response = client.get(path, params={"version": version}, headers=HEADERS)
            assert response.status_code == 409
            assert response.json() == {
                "error": {
                    "code": "unsupported_workflow_version",
                    "message": f"unsupported workflow version: {version!r}",
                    "details": {
                        "requested_version": version,
                        "current_version": current_version,
                    },
                }
            }
