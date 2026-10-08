"""The candidate-evaluation prompt: bounded batch, honest runs, shortlist."""

from __future__ import annotations

from thesis.agent.runtime.program_tools import PROGRAM_TOOL_NAMES
from thesis.workflows.registry.hypothesis_development.prompts.candidates import (
    _candidates_instructions,
)
from thesis.workflows.registry.hypothesis_development.spec import phase_declarations

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


def test_candidates_prompt_bounds_the_batch_and_requires_honest_runs() -> None:
    prompt = _candidates_instructions()
    assert "at most" in prompt
    assert "16 distinct parameter assignments" in prompt
    assert "run_backtest_batch" in prompt
    assert "read_strategy" in prompt
    assert "get_job_status" in prompt and "get_job_result" in prompt
    assert "jobId" in prompt and "unknown" in prompt
    assert "never invent" in prompt
    assert "null" in prompt


def test_candidates_prompt_selects_on_evidence_not_top_return() -> None:
    prompt = _candidates_instructions()
    for criterion in (
        "net total return",
        "profit factor",
        "drawdown",
        "trade count",
        "turnover",
        "diversity",
    ):
        assert criterion in prompt
    assert "selectionRule" in prompt
    assert "rationale" in prompt
    assert "limitations" in prompt


def test_candidates_prompt_contract_targets_and_wire_keys() -> None:
    prompt = _candidates_instructions()
    assert '"null_testing"' in prompt
    assert "strategy_rca" in prompt
    assert '"failed"' in prompt
    assert "ordinary negative result" in prompt
    assert "{strategy, batchId, assignments, shortlist, selectionRule" in prompt
    assert "optimization" in prompt


def test_candidates_prompt_names_only_allowlisted_tools() -> None:
    declarations = {item.spec.phase: item for item in phase_declarations()}
    optimization = declarations["optimization"]
    allowed = (
        set(optimization.tools)
        | set(optimization.mcp_tools)
        | {
            "request_phase_completion",
            "request_workflow_input",
        }
    )

    prompt = _candidates_instructions()
    named = {tool for tool in _KNOWN_TOOLS if tool in prompt}

    assert named
    assert named <= allowed, sorted(named - allowed)
