"""The null-testing prompt: one null batch, honest replicates, computed p."""

from __future__ import annotations

from thesis.agent.runtime.program_tools import PROGRAM_TOOL_NAMES
from thesis.workflows.registry.hypothesis_development.prompts.nulls import (
    _nulls_instructions,
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


def test_nulls_prompt_uses_the_shortlist_and_one_null_batch() -> None:
    prompt = _nulls_instructions()
    assert "shortlist" in prompt
    assert "run_backtest" in prompt
    assert "run_null_batch" in prompt
    assert "exactly once" in prompt
    assert "block_size" in prompt and "seeds" in prompt
    assert "block_permutation" in prompt
    assert "get_job_status" in prompt and "get_job_result" in prompt
    assert "never invent" in prompt


def test_nulls_prompt_computes_exceedance_and_pvalue_from_returns() -> None:
    prompt = _nulls_instructions()
    assert "exceedanceCount" in prompt
    assert "(1 + exceedanceCount) / (1 + succeededReplicateCount)" in prompt
    assert '"upper"' in prompt and '"lower"' in prompt
    assert "succeeded replicates only" in prompt
    assert "limitations" in prompt
    assert "unknown outcome" in prompt


def test_nulls_prompt_contract_targets_and_wire_keys() -> None:
    prompt = _nulls_instructions()
    assert '"variation"' in prompt
    assert "strategy_rca" in prompt
    assert '"failed"' in prompt
    assert (
        "{strategy, batchId, mode, blockSize, statistic, direction, observed, "
        "replicates, exceedanceCount, pValue, limitations}" in prompt
    )
    assert "request_phase_completion" in prompt


def test_nulls_prompt_names_only_allowlisted_tools() -> None:
    declarations = {item.spec.phase: item for item in phase_declarations()}
    null_testing = declarations["null_testing"]
    allowed = (
        set(null_testing.tools)
        | set(null_testing.mcp_tools)
        | {
            "request_phase_completion",
            "request_workflow_input",
        }
    )

    prompt = _nulls_instructions()
    named = {tool for tool in _KNOWN_TOOLS if tool in prompt}

    assert named
    assert named <= allowed, sorted(named - allowed)
