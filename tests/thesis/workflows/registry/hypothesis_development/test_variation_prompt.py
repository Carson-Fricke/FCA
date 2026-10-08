"""The variation prompt: one neighborhood batch, honest points, stability."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from thesis.agent.runtime.program_tools import PROGRAM_TOOL_NAMES
from thesis.workflows.core.phase import OneShotPhase
from thesis.workflows.core.spec import HandoffContext
from thesis.workflows.registry.hypothesis_development.contracts.candidates import (
    CandidateEvaluation,
)
from thesis.workflows.registry.hypothesis_development.prompts.variation import (
    _variation_instructions,
)
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_workflow,
    phase_declarations,
)

from ._support import _candidate_evaluation_envelope

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


def test_variation_prompt_reads_the_declaration_and_the_center() -> None:
    prompt = _variation_instructions()
    assert "read_strategy" in prompt
    assert "parameter declaration" in prompt
    assert "shortlist" in prompt
    assert "run_backtest" in prompt
    assert "center" in prompt
    assert "exactly once" in prompt


def test_variation_prompt_builds_one_at_a_time_neighborhood() -> None:
    prompt = _variation_instructions()
    assert "run_backtest_batch" in prompt
    assert "one parameter at a time" in prompt
    assert "exactly one key" in prompt
    assert "1 to 16" in prompt
    assert "bounds/levels" in prompt
    assert "cover every parameter" in prompt
    assert "Never vary two keys" in prompt


def test_variation_prompt_inspects_every_point_and_copies_metrics() -> None:
    prompt = _variation_instructions()
    assert "get_job_status" in prompt and "get_job_result" in prompt
    assert "never invent" in prompt
    assert "unknown outcome" in prompt
    assert "succeeded" in prompt and "failed" in prompt and "running" in prompt
    assert "plateau" in prompt
    assert "limitations" in prompt


def test_variation_prompt_contract_targets_and_wire_keys() -> None:
    prompt = _variation_instructions()
    assert 'target: "forward_validation"' in prompt
    assert "strategy_rca" in prompt
    assert '"failed"' in prompt
    assert (
        "{strategy, batchId, center, points, parameters, stability, "
        "limitations}" in prompt
    )
    assert "request_phase_completion" in prompt


def test_variation_prompt_names_only_allowlisted_tools() -> None:
    declarations = {item.spec.phase: item for item in phase_declarations()}
    variation = declarations["variation"]
    allowed = (
        set(variation.tools)
        | set(variation.mcp_tools)
        | {
            "request_phase_completion",
            "request_workflow_input",
        }
    )

    prompt = _variation_instructions()
    named = {tool for tool in _KNOWN_TOOLS if tool in prompt}

    assert named
    assert named <= allowed, sorted(named - allowed)


def test_variation_seed_carries_the_recorded_candidate_evaluation() -> None:
    workflow = build_hypothesis_development_workflow()
    phase = cast(OneShotPhase, workflow.spec.phase_specs["variation"])
    assert phase.seed is not None
    evaluation = CandidateEvaluation.from_dict(_candidate_evaluation_envelope())
    state = cast(Any, SimpleNamespace(candidate_evaluation=evaluation))
    prompt = phase.seed(
        workflow,
        cast(Any, None),
        state,
        HandoffContext(source="null_testing", target="variation"),
    )
    assert "Recorded candidate evaluation:" in prompt
    assert "strategy-momentum-scope-run-1" in prompt
    assert "shortlist" in prompt
