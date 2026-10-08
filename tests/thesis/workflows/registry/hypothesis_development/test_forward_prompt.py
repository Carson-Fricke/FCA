"""The forward prompt: one frozen design, protected window, honest comparison."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from thesis.agent.runtime.program_tools import PROGRAM_TOOL_NAMES
from thesis.workflows.core.phase import OneShotPhase
from thesis.workflows.core.spec import HandoffContext
from thesis.workflows.registry.hypothesis_development.contracts.forward import (
    ForwardEvaluation,
)
from thesis.workflows.registry.hypothesis_development.contracts.nulls import (
    NullTestEvaluation,
)
from thesis.workflows.registry.hypothesis_development.contracts.variation import (
    VariationEvaluation,
)
from thesis.workflows.registry.hypothesis_development.prompts.forward import (
    _forward_instructions,
)
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_workflow,
    phase_declarations,
)

from ._support import _forward_envelope, _null_test_envelope, _variation_envelope

_KNOWN_TOOLS = set(PROGRAM_TOOL_NAMES) | {
    "get_job_status",
    "get_job_result",
    "list_artifacts",
    "read_artifact",
    "search_knowledge",
    "read_knowledge",
    "request_phase_completion",
    "request_workflow_input",
}


def test_forward_prompt_reuses_the_frozen_center_null_and_variation() -> None:
    prompt = _forward_instructions()
    assert "variation handoff" in prompt
    assert "recorded null test" in prompt
    assert "center" in prompt
    assert "run_backtest" in prompt
    assert "run_null_batch" in prompt
    assert "run_backtest_batch" in prompt
    assert "block_size" in prompt and "seeds" in prompt
    assert "exactly once" in prompt
    assert "same" in prompt


def test_forward_prompt_uses_the_protected_window_without_asking() -> None:
    prompt = _forward_instructions()
    assert "protected forward window" in prompt
    assert "applied automatically" in prompt
    assert "never ask the operator for dates" in prompt
    assert "never widen" in prompt
    assert "shift" in prompt


def test_forward_prompt_compares_honestly_without_retuning() -> None:
    prompt = _forward_instructions()
    assert "development-versus-forward" in prompt
    assert "Retuning on forward data is forbidden" in prompt
    assert "report degradation" in prompt
    assert "get_job_status" in prompt and "get_job_result" in prompt
    assert "never invent" in prompt
    assert "unknown outcome" in prompt
    assert "exceedanceCount" in prompt
    assert "(1 + exceedanceCount) / (1 + succeededReplicateCount)" in prompt
    assert "limitations" in prompt


def test_forward_prompt_contract_targets_and_wire_keys() -> None:
    prompt = _forward_instructions()
    assert 'target: "completed"' in prompt
    assert "strategy_rca" in prompt
    assert '"failed"' in prompt
    assert (
        "{strategy, window, statistic, direction, observed, null, variation, "
        "comparison, limitations}" in prompt
    )
    assert "request_phase_completion" in prompt


def test_forward_prompt_names_only_allowlisted_tools() -> None:
    declarations = {item.spec.phase: item for item in phase_declarations()}
    forward = declarations["forward_validation"]
    allowed = (
        set(forward.tools)
        | set(forward.mcp_tools)
        | {
            "request_phase_completion",
            "request_workflow_input",
        }
    )

    prompt = _forward_instructions()
    named = {tool for tool in _KNOWN_TOOLS if tool in prompt}

    assert named
    assert named <= allowed, sorted(named - allowed)


def test_forward_seed_carries_both_recorded_designs() -> None:
    workflow = build_hypothesis_development_workflow()
    phase = cast(OneShotPhase, workflow.spec.phase_specs["forward_validation"])
    assert phase.seed is not None
    state = cast(
        Any,
        SimpleNamespace(
            null_testing=NullTestEvaluation.from_dict(_null_test_envelope()),
            variation=VariationEvaluation.from_dict(_variation_envelope()),
        ),
    )
    prompt = phase.seed(
        workflow,
        cast(Any, None),
        state,
        HandoffContext(
            source="variation",
            target="forward_validation",
            completion={"variation": _variation_envelope()},
        ),
    )
    assert "Recorded null test:" in prompt
    assert "Recorded variation:" in prompt
    assert "null-momentum-0123456789ab" in prompt
    assert "strategy-momentum-scope-var-1" in prompt
    assert "Accepted completion from the variation phase:" in prompt


def test_forward_seed_tolerates_an_empty_state() -> None:
    workflow = build_hypothesis_development_workflow()
    phase = cast(OneShotPhase, workflow.spec.phase_specs["forward_validation"])
    assert phase.seed is not None
    prompt = phase.seed(
        workflow,
        cast(Any, None),
        cast(Any, None),
        HandoffContext(source="variation", target="forward_validation"),
    )
    assert "Forward-validation protocol" in prompt
    assert "Recorded null test:" not in prompt


def test_forward_evaluation_fixture_matches_the_contract() -> None:
    evaluation = ForwardEvaluation.from_dict(_forward_envelope())
    assert evaluation.null.p_value == 2 / 3
    assert evaluation.variation.center.parameters["fast"] == 5
