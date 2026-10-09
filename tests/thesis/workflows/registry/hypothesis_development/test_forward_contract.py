"""The forward out-of-sample validation contract: decode, validation, folds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

import pytest

from thesis.agent.runtime.schema import validate_arguments
from thesis.workflows.core.execution_store import StoreTransition
from thesis.workflows.registry.hypothesis_development.contracts.forward import (
    FORWARD_PAYLOAD_SCHEMA,
    ForwardEvaluation,
    ForwardNullEvidence,
    ForwardVariationEvidence,
    ForwardWindow,
    forward_payload_errors,
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

from ._support import WORKSPACE, _forward_envelope


def _moment() -> datetime:
    return datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _nested(payload: dict[str, object], key: str) -> dict[str, object]:
    block = payload[key]
    assert isinstance(block, dict)
    return block


def _missing_statistic(payload: dict[str, object]) -> None:
    observed = _nested(payload, "observed")
    observed["metrics"] = {"netTotalReturn": 0.05}


def _observed_value_mismatch(payload: dict[str, object]) -> None:
    observed = _nested(payload, "observed")
    observed["value"] = 0.9


def _exceedance_mismatch(payload: dict[str, object]) -> None:
    _nested(payload, "null")["exceedanceCount"] = 2


def _pvalue_mismatch(payload: dict[str, object]) -> None:
    _nested(payload, "null")["pValue"] = 0.5


def _duplicate_seed(payload: dict[str, object]) -> None:
    replicates = _nested(payload, "null")["replicates"]
    assert isinstance(replicates, list)
    entry = replicates[1]
    assert isinstance(entry, dict)
    entry["seed"] = 1


def _no_succeeded(payload: dict[str, object]) -> None:
    null = _nested(payload, "null")
    replicates = null["replicates"]
    assert isinstance(replicates, list)
    for entry in replicates:
        assert isinstance(entry, dict)
        entry["state"] = "failed"
        entry["metrics"] = None
    null["exceedanceCount"] = 0
    null["pValue"] = 1.0


def _non_finite_metric(payload: dict[str, object]) -> None:
    replicates = _nested(payload, "null")["replicates"]
    assert isinstance(replicates, list)
    entry = replicates[0]
    assert isinstance(entry, dict)
    metrics = entry["metrics"]
    assert isinstance(metrics, dict)
    metrics["sharpe"] = float("inf")


def _empty_replicates(payload: dict[str, object]) -> None:
    _nested(payload, "null")["replicates"] = []


def _multi_key_diff(payload: dict[str, object]) -> None:
    points = _nested(payload, "variation")["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    entry["parameters"] = {"fast": 4, "slow": 19}


def _uncovered_parameter(payload: dict[str, object]) -> None:
    _nested(payload, "variation")["parameters"] = ["fast", "slow", "extra"]


def _duplicate_center(payload: dict[str, object]) -> None:
    variation = _nested(payload, "variation")
    center = _nested(variation, "center")
    points = variation["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    entry["parameters"] = dict(cast(Mapping[str, object], center["parameters"]))


def _duplicate_point(payload: dict[str, object]) -> None:
    points = _nested(payload, "variation")["points"]
    assert isinstance(points, list)
    first, second = points[0], points[1]
    assert isinstance(first, dict) and isinstance(second, dict)
    second["parameters"] = dict(cast(Mapping[str, object], first["parameters"]))


def _missing_point_metrics(payload: dict[str, object]) -> None:
    points = _nested(payload, "variation")["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    entry["metrics"] = None


def _bad_window(payload: dict[str, object]) -> None:
    window = _nested(payload, "window")
    window["end"] = "not-a-date"


def _inverted_window(payload: dict[str, object]) -> None:
    window = _nested(payload, "window")
    window["end"] = window["start"]


def test_forward_round_trips_and_rejects_the_snake_case_legacy_shape() -> None:
    evaluation = ForwardEvaluation.from_dict(_forward_envelope())
    assert ForwardEvaluation.from_dict(evaluation.as_dict()) == evaluation

    portable = evaluation.as_dict()
    window = cast(dict[str, object], portable["window"])
    observed = cast(dict[str, object], portable["observed"])
    null = cast(dict[str, object], portable["null"])
    replicates = cast(list[dict[str, object]], null["replicates"])
    variation = cast(dict[str, object], portable["variation"])
    center = cast(dict[str, object], variation["center"])
    points = cast(list[dict[str, object]], variation["points"])
    legacy = {
        "strategy": portable["strategy"],
        "window": {"start": window["start"], "end": window["end"]},
        "statistic": portable["statistic"],
        "direction": portable["direction"],
        "observed": {
            "job_id": observed["jobId"],
            "metrics": observed["metrics"],
            "value": observed["value"],
        },
        "null": {
            "mode": "block_permutation",
            "batch_id": null["batchId"],
            "block_size": null["blockSize"],
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
            "exceedance_count": null["exceedanceCount"],
            "p_value": null["pValue"],
        },
        "variation": {
            "batch_id": variation["batchId"],
            "center": {
                "parameters": center["parameters"],
                "job_id": center["jobId"],
                "state": center["state"],
                "metrics": center["metrics"],
                "error": center["error"],
            },
            "points": [
                {
                    "parameters": item["parameters"],
                    "job_id": item["jobId"],
                    "state": item["state"],
                    "metrics": item["metrics"],
                    "error": item["error"],
                }
                for item in points
            ],
            "parameters": variation["parameters"],
            "stability": variation["stability"],
        },
        "comparison": portable["comparison"],
        "limitations": portable["limitations"],
    }

    with pytest.raises(WorkflowError):
        ForwardEvaluation.from_dict(legacy)


def test_forward_nested_evidence_round_trips_individually() -> None:
    evaluation = ForwardEvaluation.from_dict(_forward_envelope())
    assert isinstance(evaluation.window, ForwardWindow)
    assert isinstance(evaluation.null, ForwardNullEvidence)
    assert isinstance(evaluation.variation, ForwardVariationEvidence)
    assert evaluation.window.as_dict() == {
        "start": "2026-06-06T12:00:00Z",
        "end": "2026-09-04T12:00:00Z",
    }
    assert evaluation.null.as_dict()["exceedanceCount"] == 1
    assert evaluation.variation.as_dict()["parameters"] == ["fast", "slow"]


def test_forward_completion_schema_and_validator_accept_the_evaluation() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs[
        "forward_validation"
    ]
    completion = {
        "target": "completed",
        "forwardValidation": _forward_envelope(),
    }
    assert phase.validate is not None and phase.validate(completion) is None
    assert not validate_arguments(phase.completion_schema, completion)
    schema = phase.completion_schema
    assert schema is not None
    properties = cast(Mapping[str, object], schema["properties"])
    assert properties["forwardValidation"] is FORWARD_PAYLOAD_SCHEMA
    target = cast(Mapping[str, object], properties["target"])
    assert target["enum"] == ["completed", "strategy_rca"]
    assert "forwardValidation" in cast(Sequence[str], schema["required"])


@pytest.mark.parametrize(
    ("mutate", "marker"),
    [
        (_missing_statistic, "must contain a finite number at 'sharpe'"),
        (_observed_value_mismatch, "must equal"),
        (_exceedance_mismatch, "extremeness count"),
        (_pvalue_mismatch, "p_value must equal"),
        (_duplicate_seed, "seeds must be unique"),
        (_no_succeeded, "at least one succeeded replicate"),
        (_non_finite_metric, "must be finite numbers"),
        (_empty_replicates, "at least 1 item"),
        (_multi_key_diff, "exactly one parameter"),
        (_uncovered_parameter, "is not varied by any point"),
        (_duplicate_center, "must not repeat the center"),
        (_duplicate_point, "parameter vectors must be unique"),
        (_missing_point_metrics, "object for a succeeded point"),
        (_bad_window, "RFC3339"),
        (_inverted_window, "must precede"),
    ],
)
def test_forward_evaluation_rejections(mutate, marker: str) -> None:
    payload = _forward_envelope()
    mutate(payload)

    errors = forward_payload_errors(payload)
    assert any(marker in error for error in errors), errors

    phase = build_hypothesis_development_workflow().spec.phase_specs[
        "forward_validation"
    ]
    assert phase.validate is not None
    failure = phase.validate({"target": "completed", "forwardValidation": payload})
    assert failure is not None and marker in failure

    with pytest.raises(WorkflowError):
        ForwardEvaluation.from_dict(payload)


def test_forward_evaluation_rejects_foreign_target_and_wrong_strategy_kind() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs[
        "forward_validation"
    ]
    assert phase.validate is not None
    completion = {
        "target": "variation",
        "forwardValidation": _forward_envelope(),
    }
    assert validate_arguments(phase.completion_schema, completion)

    payload = _forward_envelope()
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["kind"] = "study"
    assert any(
        "must be 'strategy'" in error for error in forward_payload_errors(payload)
    )
    with pytest.raises(WorkflowError):
        ForwardEvaluation.from_dict(payload)


@pytest.mark.parametrize("target", ["completed", "strategy_rca"])
def test_forward_fold_reconstructs_after_pure_rebuild(target: str) -> None:
    evaluation = ForwardEvaluation.from_dict(_forward_envelope())
    transition = StoreTransition(
        sequence=1,
        from_phase="forward_validation",
        to_phase=target,
        occurred_at=_moment(),
        intent_epoch=1,
        reason="forward validation accepted",
        completion={"target": target, "forwardValidation": evaluation.as_dict()},
    )
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-forward-validation",
        phase=HypothesisDevelopmentPhase.FORWARD_VALIDATION,
        intent_epoch=0,
    )

    folded = fold_transition(state, transition)

    assert folded.phase.value == target
    assert folded.forward_validation == evaluation

    rebuilt = rebuild_state(
        WORKSPACE,
        "execution-forward-validation",
        [transition],
        initial_phase=HypothesisDevelopmentPhase.FORWARD_VALIDATION,
    )
    assert rebuilt.forward_validation == evaluation


def test_outcome_emits_forward_validation_and_resets_clear_it() -> None:
    evaluation = ForwardEvaluation.from_dict(_forward_envelope())
    outcome = HypothesisDevelopmentOutcome(
        workspace_id=WORKSPACE,
        execution_id="execution-transport",
        phase=HypothesisDevelopmentPhase.FORWARD_VALIDATION,
        fenced=False,
        transitions=(),
        forward_validation=evaluation,
    )
    payload = outcome.as_dict()
    assert payload["forwardValidation"] == evaluation.as_dict()
    assert payload["variation"] is None

    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-reset",
        phase=HypothesisDevelopmentPhase.FORWARD_VALIDATION,
        intent_epoch=0,
        forward_validation=evaluation,
    )
    for reset in (reset_premise, reset_discovery, reset_backtests):
        assert reset(state).forward_validation is None
