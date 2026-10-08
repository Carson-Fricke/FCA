"""Phase capabilities and structural strategy receipt completion contracts."""

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


def test_design_receipt_schema_and_validator_agree() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["strategy_design"]
    completion = {"strategy": _receipt(), "target": "strategy_backtesting"}
    assert phase.validate is not None and phase.validate(completion) is None
    assert not validate_arguments(phase.completion_schema, completion)


_BACKTEST_TOOLS = {
    "search_knowledge",
    "read_knowledge",
    "read_strategy",
    "run_backtest",
    "read_sdk_reference",
    "get_job_status",
    "get_job_result",
    "list_artifacts",
    "read_artifact",
    "request_phase_completion",
    "request_workflow_input",
}


@pytest.mark.parametrize(
    ("phase_name", "extra_tools"),
    [
        ("strategy_backtesting", set()),
        ("optimization", {"run_backtest_batch"}),
        ("null_testing", {"run_null_batch"}),
        ("variation", {"run_backtest_batch"}),
        ("forward_validation", {"run_backtest_batch", "run_null_batch"}),
    ],
)
def test_backtest_agents_have_declared_launch_status_and_evidence_tools(
    phase_name: str, extra_tools: set[str]
) -> None:
    workflow = build_hypothesis_development_workflow()
    doctrine = workflow._doctrine
    assert doctrine is not None
    phase = workflow.spec.phase_specs[phase_name]
    assert isinstance(phase, OneShotPhase)
    assert doctrine.phase_mcp_tools[phase_name] == (
        "get_job_status",
        "get_job_result",
        "list_artifacts",
        "read_artifact",
    )
    assert set(_phase_allowed_tools(doctrine, phase_name, "coding")) == (
        _BACKTEST_TOOLS | extra_tools
    )
    assert phase.seed is not None
    prompt = phase.seed(
        workflow,
        cast(Any, None),
        cast(Any, None),
        HandoffContext(source="strategy_design", target=phase_name),
    )
    for tool in (
        "get_job_status",
        "get_job_result",
        "list_artifacts",
        "read_artifact",
    ):
        assert tool in prompt


def test_strategy_backtesting_prompt_requires_honest_terminal_inspection() -> None:
    workflow = build_hypothesis_development_workflow()
    phase = cast(OneShotPhase, workflow.spec.phase_specs["strategy_backtesting"])
    assert phase.seed is not None
    prompt = phase.seed(
        workflow,
        cast(Any, None),
        cast(Any, None),
        HandoffContext(source="strategy_design", target="strategy_backtesting"),
    )
    assert "Launch acceptance is not a successful completed run" in prompt
    assert "while queued or running" in prompt
    assert "read_sdk_reference" in prompt
    assert "actual executed version/gitCommit" in prompt


def test_optimization_prompt_bounds_the_batch_and_selects_on_evidence() -> None:
    workflow = build_hypothesis_development_workflow()
    phase = cast(OneShotPhase, workflow.spec.phase_specs["optimization"])
    assert phase.seed is not None
    prompt = phase.seed(
        workflow,
        cast(Any, None),
        cast(Any, None),
        HandoffContext(source="strategy_backtesting", target="optimization"),
    )
    assert "run_backtest_batch" in prompt
    assert "16 distinct parameter assignments" in prompt
    assert "jobId" in prompt and "unknown" in prompt
    assert "never invent" in prompt
    for criterion in (
        "net total return",
        "profit factor",
        "drawdown",
        "trade count",
        "turnover",
        "diversity",
    ):
        assert criterion in prompt
    assert '"null_testing"' in prompt and "strategy_rca" in prompt
    assert "optimization" in prompt and "selectionRule" in prompt


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
