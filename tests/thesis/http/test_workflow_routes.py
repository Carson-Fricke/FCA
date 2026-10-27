"""Workflow routes expose current topology, authentication and version checks."""

from typing import Any
from unittest.mock import Mock

from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis_runtime.workflows.graph.contracts import Destination
from thesis_runtime.workflows.phase import (
    ConversationPhase,
    DeterministicPhase,
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
        routes={
            "draft": {
                name: (Destination(name, "input"),) for name in ("review", "draft")
            },
            "review": {
                name: (Destination(name, "input"),) for name in ("publish", "repair")
            },
            "repair": {"draft": (Destination("draft", "input"),)},
            "publish": {},
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
                "inputs": {
                    name: ["input"] for name in ("draft", "review", "repair", "publish")
                },
                "routes": {
                    "draft": {name: [f"{name}.input"] for name in ("review", "draft")},
                    "review": {
                        name: [f"{name}.input"] for name in ("publish", "repair")
                    },
                    "repair": {"draft": ["draft.input"]},
                    "publish": {},
                },
            }
    execute.assert_not_called()


def test_definition_exposes_fanout_routes_and_named_aggregation_inputs() -> None:
    spec: WorkflowSpec[Any, Any, Any] = WorkflowSpec(
        workflow_id="parallel-review",
        version="1.0.0",
        title="Parallel review",
        phases=(
            ConversationPhase(phase="start"),
            ConversationPhase(phase="left"),
            ConversationPhase(phase="right"),
            DeterministicPhase(
                phase="join",
                inputs=("left", "right"),
                contract="combine",
                on_complete="done",
            ),
            TerminalPhase(phase="done"),
        ),
        routes={
            "start": {
                "review": (Destination("left", "input"), Destination("right", "input"))
            },
            "left": {"join": (Destination("join", "left"),)},
            "right": {"join": (Destination("join", "right"),)},
            "join": {"done": (Destination("done", "input"),)},
            "done": {},
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
        assert response.json()["routes"]["start"] == {
            "review": ["left.input", "right.input"]
        }
        assert response.json()["inputs"]["join"] == ["left", "right"]
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
        assert set(definition["inputs"]) == set(definition["phases"])
        current_version = definition["version"]
        assert (
            client.get(
                path, params={"version": current_version}, headers=HEADERS
            ).json()
            == definition
        )
        assert definition["phases"][:2] == ["discovery", "model_creation"]
        assert {"model_creation", "discovery"} <= set(definition["routes"]["verdict"])
        assert definition["terminal_phases"] == ["completed", "failed"]
        assert definition["routes"]["completed"] == {}
        assert definition["routes"]["failed"] == {}
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
