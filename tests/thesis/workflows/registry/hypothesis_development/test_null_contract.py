"""The block-permutation null-test contract: decode, validation, and folds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

import pytest

from thesis.agent.runtime.schema import validate_arguments
from thesis.workflows.core.execution_store import StoreTransition
from thesis.workflows.registry.hypothesis_development.contracts.nulls import (
    NULL_TEST_EVALUATION_PAYLOAD_SCHEMA,
    NullTestEvaluation,
    null_test_evaluation_payload_errors,
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

from ._support import WORKSPACE, _null_test_envelope


def _moment() -> datetime:
    return datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _missing_statistic(payload: dict[str, object]) -> None:
    observed = payload["observed"]
    assert isinstance(observed, dict)
    observed["metrics"] = {"netTotalReturn": 0.12}


def _observed_value_mismatch(payload: dict[str, object]) -> None:
    observed = payload["observed"]
    assert isinstance(observed, dict)
    observed["value"] = 2.0


def _exceedance_mismatch(payload: dict[str, object]) -> None:
    payload["exceedanceCount"] = 2


def _pvalue_mismatch(payload: dict[str, object]) -> None:
    payload["pValue"] = 0.5


def _duplicate_seed(payload: dict[str, object]) -> None:
    replicates = payload["replicates"]
    assert isinstance(replicates, list)
    entry = replicates[1]
    assert isinstance(entry, dict)
    entry["seed"] = 1


def _no_succeeded(payload: dict[str, object]) -> None:
    replicates = payload["replicates"]
    assert isinstance(replicates, list)
    for entry in replicates:
        assert isinstance(entry, dict)
        entry["state"] = "failed"
        entry["metrics"] = None
    payload["exceedanceCount"] = 0
    payload["pValue"] = 1.0


def _non_finite_metric(payload: dict[str, object]) -> None:
    replicates = payload["replicates"]
    assert isinstance(replicates, list)
    entry = replicates[0]
    assert isinstance(entry, dict)
    metrics = entry["metrics"]
    assert isinstance(metrics, dict)
    metrics["sharpe"] = float("inf")


def _empty_replicates(payload: dict[str, object]) -> None:
    payload["replicates"] = []


def _wrong_mode(payload: dict[str, object]) -> None:
    payload["mode"] = "shuffle"


def _wrong_strategy_kind(payload: dict[str, object]) -> None:
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["kind"] = "study"


def test_null_test_round_trips_and_decodes_snake_case() -> None:
    evaluation = NullTestEvaluation.from_dict(_null_test_envelope())
    assert NullTestEvaluation.from_dict(evaluation.as_dict()) == evaluation

    portable = evaluation.as_dict()
    observed = cast(dict[str, object], portable["observed"])
    replicates = cast(list[dict[str, object]], portable["replicates"])
    legacy = {
        "strategy": portable["strategy"],
        "batch_id": portable["batchId"],
        "mode": portable["mode"],
        "block_size": portable["blockSize"],
        "statistic": portable["statistic"],
        "direction": portable["direction"],
        "observed": {
            "job_id": observed["jobId"],
            "metrics": observed["metrics"],
            "value": observed["value"],
        },
        "replicates": [
            {
                "seed": item["seed"],
                "job_id": item["jobId"],
                "state": item["state"],
                "metrics": item["metrics"],
                "error": item["error"],
            }
            for item in replicates
        ],
        "exceedance_count": portable["exceedanceCount"],
        "p_value": portable["pValue"],
        "limitations": portable["limitations"],
    }

    assert NullTestEvaluation.from_dict(legacy) == evaluation


def test_null_test_completion_schema_and_validator_accept_the_evaluation() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["null_testing"]
    completion = {
        "target": "variation",
        "nullTesting": _null_test_envelope(),
    }
    assert phase.validate is not None and phase.validate(completion) is None
    assert not validate_arguments(phase.completion_schema, completion)
    schema = phase.completion_schema
    assert schema is not None
    properties = cast(Mapping[str, object], schema["properties"])
    assert properties["nullTesting"] is NULL_TEST_EVALUATION_PAYLOAD_SCHEMA
    target = cast(Mapping[str, object], properties["target"])
    assert target["enum"] == ["variation", "strategy_rca"]
    assert "nullTesting" in cast(Sequence[str], schema["required"])


@pytest.mark.parametrize(
    ("mutate", "marker"),
    [
        (_missing_statistic, "must contain a finite number at 'sharpe'"),
        (_observed_value_mismatch, "must equal"),
        (_exceedance_mismatch, "extremeness count"),
        (_pvalue_mismatch, "pValue must equal"),
        (_duplicate_seed, "seeds must be unique"),
        (_no_succeeded, "at least one succeeded replicate"),
        (_non_finite_metric, "must be finite numbers"),
        (_empty_replicates, "at least 1 item"),
        (_wrong_mode, "is not one of"),
        (_wrong_strategy_kind, "must be 'strategy'"),
    ],
)
def test_null_test_evaluation_rejections(mutate, marker: str) -> None:
    payload = _null_test_envelope()
    mutate(payload)

    errors = null_test_evaluation_payload_errors(payload)
    assert any(marker in error for error in errors), errors

    phase = build_hypothesis_development_workflow().spec.phase_specs["null_testing"]
    assert phase.validate is not None
    failure = phase.validate({"target": "variation", "nullTesting": payload})
    assert failure is not None and marker in failure

    with pytest.raises(WorkflowError):
        NullTestEvaluation.from_dict(payload)


def test_null_test_evaluation_rejects_foreign_target_and_bad_bounds() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["null_testing"]
    assert phase.validate is not None
    completion = {
        "target": "optimization",
        "nullTesting": _null_test_envelope(),
    }
    assert validate_arguments(phase.completion_schema, completion)

    payload = _null_test_envelope()
    payload["blockSize"] = 0
    assert any(
        "blockSize" in error for error in null_test_evaluation_payload_errors(payload)
    )
    with pytest.raises(WorkflowError):
        NullTestEvaluation.from_dict(payload)


@pytest.mark.parametrize("target", ["variation", "strategy_rca"])
def test_null_test_fold_reconstructs_after_pure_rebuild(target: str) -> None:
    evaluation = NullTestEvaluation.from_dict(_null_test_envelope())
    transition = StoreTransition(
        sequence=1,
        from_phase="null_testing",
        to_phase=target,
        occurred_at=_moment(),
        intent_epoch=1,
        reason="null test accepted",
        completion={"target": target, "nullTesting": evaluation.as_dict()},
    )
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-null-testing",
        phase=HypothesisDevelopmentPhase.NULL_TESTING,
        intent_epoch=0,
    )

    folded = fold_transition(state, transition)

    assert folded.phase.value == target
    assert folded.null_testing == evaluation

    rebuilt = rebuild_state(
        WORKSPACE,
        "execution-null-testing",
        [transition],
        initial_phase=HypothesisDevelopmentPhase.NULL_TESTING,
    )
    assert rebuilt.null_testing == evaluation


def test_outcome_emits_null_testing_and_resets_clear_it() -> None:
    evaluation = NullTestEvaluation.from_dict(_null_test_envelope())
    outcome = HypothesisDevelopmentOutcome(
        workspace_id=WORKSPACE,
        execution_id="execution-transport",
        phase=HypothesisDevelopmentPhase.NULL_TESTING,
        fenced=False,
        transitions=(),
        null_testing=evaluation,
    )
    payload = outcome.as_dict()
    assert payload["nullTesting"] == evaluation.as_dict()
    assert payload["candidateEvaluation"] is None

    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-reset",
        phase=HypothesisDevelopmentPhase.NULL_TESTING,
        intent_epoch=0,
        null_testing=evaluation,
    )
    for reset in (reset_premise, reset_discovery, reset_backtests):
        assert reset(state).null_testing is None
