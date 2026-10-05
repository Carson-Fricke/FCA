"""Phase capabilities and structural strategy receipt completion contracts."""

from dataclasses import replace
from typing import Any, cast

import pytest

from thesis.agent.runtime.schema import validate_arguments
from thesis.workflows.core.phase import OneShotPhase
from thesis.workflows.core.spec import HandoffContext
from thesis.workflows.core.workflow import _phase_allowed_tools
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_workflow,
)


def _receipt() -> dict[str, object]:
    return {
        "workspaceId": "workspace-demo",
        "kind": "strategy",
        "programId": "momentum",
        "gitCommit": "a" * 40,
        "version": "b" * 64,
    }


def test_only_design_has_authoring_grant_and_generic_default_is_empty() -> None:
    workflow = build_hypothesis_development_workflow()
    doctrine = workflow._doctrine
    assert doctrine is not None
    assert "author_strategy" in doctrine.phase_tools["strategy_design"]
    assert "read_sdk_reference" in doctrine.phase_tools["strategy_design"]
    for phase in workflow.phase_names:
        allowed = _phase_allowed_tools(doctrine, phase, "strategy")
        assert ("author_strategy" in allowed) == (
            phase in {"discovery", "model_creation", "strategy_design"}
        )
    assert "author_strategy" not in _phase_allowed_tools(
        replace(doctrine, phase_tools={}), "strategy_design", "strategy"
    )
    custom = replace(doctrine, phase_tools={"custom": ("local_tool", "local_tool")})
    assert _phase_allowed_tools(custom, "custom", "strategy").count("local_tool") == 1


def test_design_receipt_schema_and_validator_agree() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["strategy_design"]
    completion = {"strategy": _receipt(), "target": "strategy_backtesting"}
    assert phase.validate is not None and phase.validate(completion) is None
    assert not validate_arguments(phase.completion_schema, completion)


@pytest.mark.parametrize("phase_name", ["strategy_backtesting", "optimization"])
def test_backtest_agents_have_declared_launch_status_and_evidence_tools(
    phase_name: str,
) -> None:
    workflow = build_hypothesis_development_workflow()
    doctrine = workflow._doctrine
    assert doctrine is not None
    phase = workflow.spec.phase_specs[phase_name]
    assert isinstance(phase, OneShotPhase)
    assert doctrine.phase_mcp_tools[phase_name] == (
        "get_job_status",
        "get_run",
        "list_artifacts",
        "read_artifact",
    )
    assert set(_phase_allowed_tools(doctrine, phase_name, "coding")) == {
        "search_knowledge",
        "read_knowledge",
        "read_strategy",
        "run_backtest",
        "read_sdk_reference",
        "get_job_status",
        "get_run",
        "list_artifacts",
        "read_artifact",
        "request_phase_completion",
        "request_workflow_input",
    }
    assert phase.seed is not None
    prompt = phase.seed(
        workflow,
        cast(Any, None),
        cast(Any, None),
        HandoffContext(source="strategy_design", target=phase_name),
    )
    assert "Launch acceptance is not a successful completed run" in prompt
    assert "while queued or running" in prompt
    for tool in (
        "get_job_status",
        "get_run",
        "list_artifacts",
        "read_artifact",
        "read_sdk_reference",
    ):
        assert tool in prompt
    assert "actual executed version/gitCommit" in prompt


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"anything": "nonempty"},
        {"strategyId": "momentum"},
        {**_receipt(), "bundleSha256": "not-a-digest"},
        {**_receipt(), "version": 1},
        {**_receipt(), "gitCommit": "a" * 41},
        {**_receipt(), "kind": "study"},
        {**_receipt(), "strategyId": " "},
        {**_receipt(), "artifactPath": "../outside/manifest.json"},
        {**_receipt(), "artifactPath": "strategies/../1.0.0"},
        {**_receipt(), "validation": {"accepted": False}},
    ],
)
def test_design_rejects_arbitrary_or_malformed_receipts(
    receipt: dict[str, object],
) -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["strategy_design"]
    assert phase.validate is not None
    assert phase.validate({"strategy": receipt}) is not None
