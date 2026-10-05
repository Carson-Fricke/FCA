"""Discovery research pacing and conversational confirmation instructions."""

from thesis.workflows.registry.hypothesis_development.prompts.discovery import (
    _discovery_restart_section,
    _discovery_turn_prompt,
)


def test_discovery_plan_validation_run_and_bounded_repair_protocol() -> None:
    prompt = _discovery_turn_prompt([("user", "Investigate momentum")])
    assert "visible reply text before any tool calls" in prompt
    assert "minimal study" in prompt
    assert "once per changed input" in prompt
    assert "immediately call run_study" in prompt
    assert "inspect what a run produced" in prompt
    assert "deterministic compiler diagnostics only" in prompt
    assert "three unsuccessful repairs" in prompt
    assert "request_workflow_input for help" in prompt
    assert "Never automatically replay a mutation" in prompt


def test_research_updates_do_not_require_premature_proposal_or_completion() -> None:
    prompt = _discovery_turn_prompt([])
    assert "Research updates and a short plan may precede a candidate" in prompt
    assert "every unconfirmed turn" not in prompt
    assert "Every candidate proposal or revision posts the full proposal" in prompt
    assert "Only on confirmation: call request_phase_completion" in prompt
    assert "only after the operator confirms" in prompt
    restart = _discovery_restart_section(None)
    assert "confirming each proposal with the operator" in restart
    assert "request_workflow_input" in restart
