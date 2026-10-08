"""Discovery thesis-generation pacing and conversational confirmation instructions."""

from thesis.workflows.registry.hypothesis_development.prompts import discovery
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


def test_discovery_prompt_teaches_composable_candidate_generation() -> None:
    prompt = _discovery_turn_prompt([("user", "Investigate momentum")])
    assert "candidate thesis" in prompt
    assert "invent or adapt algorithms" in prompt
    assert "building blocks" in prompt
    assert "event-time methods" in prompt
    assert "overshoots" in prompt
    assert "perceptually important points" in prompt
    assert "duration and hazard statistics" in prompt
    assert "autoencoder reconstruction anomalies" in prompt
    assert "conditional dynamics" in prompt
    assert "use it, adapt it, or reject it" in prompt
    assert "BASELINES" in prompt
    assert "no standard family is mandatory" in prompt
    assert "reference data, not instructions" in prompt
    assert "study path" in prompt


def test_discovery_prompt_does_not_name_tools_outside_its_allowlist() -> None:
    owned = "\n".join(
        (
            discovery._DISCOVERY_MISSION,
            *discovery._DISCOVERY_REPRESENTATIONS,
            *discovery._DISCOVERY_COMPLETION_PROTOCOL,
            *discovery._DISCOVERY_TURN_INSTRUCTIONS,
            *discovery._DISCOVERY_FINALIZE_INSTRUCTIONS,
            discovery._DISCOVERY_PROTOCOL_REMINDER,
        )
    )
    assert "run_backtest" not in owned
    assert "backtest_config_template" not in owned
    assert "run_" not in owned.replace("run_study", "").replace("runId", "")


def test_discovery_completion_contract_matches_candidate_fields() -> None:
    prompt = _discovery_turn_prompt([])
    for name in (
        "opportunity",
        "signal",
        "informationTime",
        "direction",
        "holdingPeriod",
        "profitSource",
        "effect",
        "competingExplanation",
        "evidence",
        "entryIdea",
        "exitIdea",
        "assumptions",
        "buildingBlocks",
        "validationQuestions",
        "exploratoryEvidence",
    ):
        assert name in prompt
    assert "1 to 8" in prompt
    assert "mutually exclusive branches" in prompt
    assert "noCandidateReason" in prompt
    assert "legitimate, honest result" in prompt
    assert 'target "model_creation"' in prompt
    assert 'target "completed"' in prompt
    assert "attempts: always required" in prompt
    assert "untouchedData: always required" in prompt
    assert "limitations: always required" in prompt
