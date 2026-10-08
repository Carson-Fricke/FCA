"""The bounded candidate-evaluation contract: decode, validation, and folds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

import pytest

from thesis.agent.runtime.schema import validate_arguments
from thesis.workflows.core.execution_store import StoreTransition
from thesis.workflows.registry.hypothesis_development.contracts.candidates import (
    CANDIDATE_EVALUATION_PAYLOAD_SCHEMA,
    CandidateEvaluation,
    candidate_evaluation_payload_errors,
)
from thesis.workflows.registry.hypothesis_development.errors import WorkflowError
from thesis.workflows.registry.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
)
from thesis.workflows.registry.hypothesis_development.piping import (
    reset_backtests,
    reset_discovery,
    reset_premise,
)
from thesis.workflows.registry.hypothesis_development.rebuild import (
    fold_transition,
    rebuild_state,
)
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_workflow,
)
from thesis.workflows.registry.hypothesis_development.state import (
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentState,
)

from ._support import WORKSPACE, _candidate_evaluation_envelope


def _moment() -> datetime:
    return datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _empty_assignments(payload: dict[str, object]) -> None:
    payload["assignments"] = []


def _shortlist_outside_assignments(payload: dict[str, object]) -> None:
    shortlist = payload["shortlist"]
    assert isinstance(shortlist, list)
    entry = shortlist[0]
    assert isinstance(entry, dict)
    entry["parameters"] = {"fast": 7}


def _non_finite_metrics(payload: dict[str, object]) -> None:
    assignments = payload["assignments"]
    assert isinstance(assignments, list)
    entry = assignments[0]
    assert isinstance(entry, dict)
    entry["metrics"] = {"netTotalReturn": float("inf")}


def _wrong_strategy_kind(payload: dict[str, object]) -> None:
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["kind"] = "study"


def test_candidate_evaluation_round_trips_and_decodes_snake_case() -> None:
    evaluation = CandidateEvaluation.from_dict(_candidate_evaluation_envelope())
    assert CandidateEvaluation.from_dict(evaluation.as_dict()) == evaluation

    portable = evaluation.as_dict()
    assignments = portable["assignments"]
    shortlist = portable["shortlist"]
    assert isinstance(assignments, list) and isinstance(shortlist, list)
    legacy = {
        "strategy": portable["strategy"],
        "batch_id": portable["batchId"],
        "assignments": [
            {
                "parameters": item["parameters"],
                "job_id": item["jobId"],
                "state": item["state"],
                "metrics": item["metrics"],
                "error": item["error"],
            }
            for item in assignments
        ],
        "shortlist": [
            {
                "parameters": item["parameters"],
                "job_id": item["jobId"],
                "rationale": item["rationale"],
            }
            for item in shortlist
        ],
        "selection_rule": portable["selectionRule"],
        "limitations": portable["limitations"],
    }

    assert CandidateEvaluation.from_dict(legacy) == evaluation


def test_candidate_completion_schema_and_validator_accept_the_evaluation() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["optimization"]
    completion = {
        "target": "null_testing",
        "optimization": _candidate_evaluation_envelope(),
    }
    assert phase.validate is not None and phase.validate(completion) is None
    assert not validate_arguments(phase.completion_schema, completion)
    schema = phase.completion_schema
    assert schema is not None
    properties = cast(Mapping[str, object], schema["properties"])
    assert properties["optimization"] is CANDIDATE_EVALUATION_PAYLOAD_SCHEMA
    target = cast(Mapping[str, object], properties["target"])
    assert target["enum"] == ["null_testing", "strategy_rca"]
    assert "optimization" in cast(Sequence[str], schema["required"])


@pytest.mark.parametrize(
    ("mutate", "marker"),
    [
        (_empty_assignments, "at least 1 item"),
        (_shortlist_outside_assignments, "must match one of the submitted assignment"),
        (_non_finite_metrics, "must be finite numbers"),
        (_wrong_strategy_kind, "must be 'strategy'"),
    ],
)
def test_candidate_evaluation_rejections(mutate, marker: str) -> None:
    payload = _candidate_evaluation_envelope()
    mutate(payload)

    errors = candidate_evaluation_payload_errors(payload)
    assert any(marker in error for error in errors)

    phase = build_hypothesis_development_workflow().spec.phase_specs["optimization"]
    assert phase.validate is not None
    failure = phase.validate({"target": "null_testing", "optimization": payload})
    assert failure is not None and marker in failure

    with pytest.raises(WorkflowError):
        CandidateEvaluation.from_dict(payload)


def test_candidate_evaluation_rejects_foreign_target_and_empty_shortlist() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["optimization"]
    assert phase.validate is not None
    completion = {
        "target": "discovery",
        "optimization": _candidate_evaluation_envelope(),
    }
    assert validate_arguments(phase.completion_schema, completion)

    payload = _candidate_evaluation_envelope()
    payload["shortlist"] = []
    with pytest.raises(WorkflowError):
        CandidateEvaluation.from_dict(payload)


@pytest.mark.parametrize("target", ["null_testing", "strategy_rca"])
def test_optimization_fold_reconstructs_after_pure_rebuild(target: str) -> None:
    evaluation = CandidateEvaluation.from_dict(_candidate_evaluation_envelope())
    transition = StoreTransition(
        sequence=1,
        from_phase="optimization",
        to_phase=target,
        occurred_at=_moment(),
        intent_epoch=1,
        reason="candidate evaluation accepted",
        completion={"target": target, "optimization": evaluation.as_dict()},
    )
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-optimization",
        phase=HypothesisDevelopmentPhase.OPTIMIZATION,
        intent_epoch=0,
    )

    folded = fold_transition(state, transition)

    assert folded.phase.value == target
    assert folded.candidate_evaluation == evaluation

    rebuilt = rebuild_state(
        WORKSPACE,
        "execution-optimization",
        [transition],
        initial_phase=HypothesisDevelopmentPhase.OPTIMIZATION,
    )
    assert rebuilt.candidate_evaluation == evaluation


def test_outcome_emits_candidate_evaluation_and_resets_clear_it() -> None:
    evaluation = CandidateEvaluation.from_dict(_candidate_evaluation_envelope())
    outcome = HypothesisDevelopmentOutcome(
        workspace_id=WORKSPACE,
        execution_id="execution-transport",
        phase=HypothesisDevelopmentPhase.OPTIMIZATION,
        fenced=False,
        transitions=(),
        candidate_evaluation=evaluation,
    )
    payload = outcome.as_dict()
    assert payload["candidateEvaluation"] == evaluation.as_dict()
    assert payload["strategy"] is None

    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-reset",
        phase=HypothesisDevelopmentPhase.OPTIMIZATION,
        intent_epoch=0,
        candidate_evaluation=evaluation,
    )
    for reset in (reset_premise, reset_discovery, reset_backtests):
        assert reset(state).candidate_evaluation is None
