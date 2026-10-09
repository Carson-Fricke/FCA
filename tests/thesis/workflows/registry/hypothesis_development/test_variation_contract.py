"""The deterministic parameter-variation contract: decode, validation, folds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

import pytest

from thesis.agent.runtime.schema import validate_arguments
from thesis.workflows.core.execution_store import StoreTransition
from thesis.workflows.registry.hypothesis_development.artifacts import (
    variation_report_artifact,
)
from thesis.workflows.registry.hypothesis_development.contracts.variation import (
    VARIATION_PAYLOAD_SCHEMA,
    VariationCenter,
    VariationEvaluation,
    VariationPoint,
    canonical_parameter_identity,
    variation_payload_errors,
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

from ._support import WORKSPACE, _variation_envelope


def _moment() -> datetime:
    return datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _multi_key_diff(payload: dict[str, object]) -> None:
    points = payload["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    entry["parameters"] = {"fast": 4, "slow": 19}


def _uncovered_parameter(payload: dict[str, object]) -> None:
    payload["parameters"] = ["fast", "slow", "extra"]


def _duplicate_center(payload: dict[str, object]) -> None:
    center = payload["center"]
    assert isinstance(center, dict)
    points = payload["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    entry["parameters"] = dict(cast(Mapping[str, object], center["parameters"]))


def _duplicate_point(payload: dict[str, object]) -> None:
    points = payload["points"]
    assert isinstance(points, list)
    first, second = points[0], points[1]
    assert isinstance(first, dict) and isinstance(second, dict)
    second["parameters"] = dict(cast(Mapping[str, object], first["parameters"]))


def _missing_center_metrics(payload: dict[str, object]) -> None:
    center = payload["center"]
    assert isinstance(center, dict)
    center["metrics"] = None


def _missing_point_metrics(payload: dict[str, object]) -> None:
    points = payload["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    entry["metrics"] = None


def _non_finite_metric(payload: dict[str, object]) -> None:
    points = payload["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    metrics = entry["metrics"]
    assert isinstance(metrics, dict)
    metrics["netTotalReturn"] = float("inf")


def _non_finite_parameter(payload: dict[str, object]) -> None:
    points = payload["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    entry["parameters"] = {"fast": float("inf"), "slow": 20}


def _non_scalar_parameter(payload: dict[str, object]) -> None:
    points = payload["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    entry["parameters"] = {"fast": {"nested": 4}, "slow": 20}


def _wrong_strategy_kind(payload: dict[str, object]) -> None:
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["kind"] = "study"


def _empty_points(payload: dict[str, object]) -> None:
    payload["points"] = []


def _empty_parameters(payload: dict[str, object]) -> None:
    payload["parameters"] = []


def _blank_stability(payload: dict[str, object]) -> None:
    payload["stability"] = "   "


def _center_not_succeeded(payload: dict[str, object]) -> None:
    center = payload["center"]
    assert isinstance(center, dict)
    center["state"] = "running"


def _missing_center_run(payload: dict[str, object]) -> None:
    center = payload["center"]
    assert isinstance(center, dict)
    del center["jobId"]


def _snake_case_wire(portable: Mapping[str, object]) -> dict[str, object]:
    """Respell every camelCase wire key of one portable variation in snake_case."""
    center = cast(dict[str, object], portable["center"])
    points = cast(list[dict[str, object]], portable["points"])
    return {
        "strategy": portable["strategy"],
        "batch_id": portable["batchId"],
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
        "parameters": portable["parameters"],
        "stability": portable["stability"],
        "limitations": portable["limitations"],
    }


def test_variation_round_trips_on_camel_case_wire_keys() -> None:
    evaluation = VariationEvaluation.from_dict(_variation_envelope())
    assert VariationEvaluation.from_dict(evaluation.as_dict()) == evaluation


def test_variation_rejects_snake_case_wire_keys() -> None:
    portable = VariationEvaluation.from_dict(_variation_envelope()).as_dict()
    legacy = _snake_case_wire(portable)

    assert variation_payload_errors(legacy)
    phase = build_hypothesis_development_workflow().spec.phase_specs["variation"]
    assert phase.validate is not None
    failure = phase.validate({"target": "forward_validation", "variation": legacy})
    assert failure is not None

    with pytest.raises(WorkflowError, match="jobId"):
        VariationEvaluation.from_dict(legacy)


def test_variation_center_and_points_round_trip_individually() -> None:
    evaluation = VariationEvaluation.from_dict(_variation_envelope())
    assert isinstance(evaluation.center, VariationCenter)
    assert isinstance(evaluation.points[0], VariationPoint)
    assert evaluation.center.as_dict()["state"] == "succeeded"
    assert evaluation.points[2].as_dict()["metrics"] is None
    assert evaluation.points[3].as_dict()["state"] == "running"


def test_parameter_identity_is_type_strict_and_order_insensitive() -> None:
    integer_five = canonical_parameter_identity({"fast": 5})
    float_five = canonical_parameter_identity({"fast": 5.0})
    assert integer_five != float_five
    assert canonical_parameter_identity({"fast": True}) != (
        canonical_parameter_identity({"fast": 1})
    )
    assert canonical_parameter_identity({"slow": 20, "fast": 5}) == (
        canonical_parameter_identity({"fast": 5, "slow": 20})
    )


def test_integer_and_float_values_are_distinct_variation_points() -> None:
    payload = _variation_envelope()
    points = cast(list[dict[str, object]], payload["points"])
    # The center holds integer 5. This point holds float 5.0: a different value
    # that differs from the center in exactly one declared parameter.
    points[0]["parameters"] = {"fast": 5.0, "slow": 20}

    assert variation_payload_errors(payload) == []
    phase = build_hypothesis_development_workflow().spec.phase_specs["variation"]
    assert phase.validate is not None
    completion = {"target": "forward_validation", "variation": payload}
    assert phase.validate(completion) is None

    evaluation = VariationEvaluation.from_dict(payload)
    assert type(evaluation.points[0].parameters["fast"]) is float
    assert type(evaluation.center.parameters["fast"]) is int


def test_variation_completion_schema_and_validator_accept_the_evaluation() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["variation"]
    completion = {
        "target": "forward_validation",
        "variation": _variation_envelope(),
    }
    assert phase.validate is not None and phase.validate(completion) is None
    assert not validate_arguments(phase.completion_schema, completion)
    schema = phase.completion_schema
    assert schema is not None
    properties = cast(Mapping[str, object], schema["properties"])
    assert properties["variation"] is VARIATION_PAYLOAD_SCHEMA
    target = cast(Mapping[str, object], properties["target"])
    assert target["enum"] == ["forward_validation", "strategy_rca"]
    assert "variation" in cast(Sequence[str], schema["required"])


@pytest.mark.parametrize(
    ("mutate", "marker"),
    [
        (_multi_key_diff, "exactly one parameter"),
        (_uncovered_parameter, "is not varied by any point"),
        (_duplicate_center, "must not repeat the center"),
        (_duplicate_point, "parameter vectors must be unique"),
        (_missing_center_metrics, "object for the succeeded center"),
        (_missing_point_metrics, "object for a succeeded point"),
        (_non_finite_metric, "must be finite numbers"),
        (_non_finite_parameter, "parameter values must be finite"),
        (_non_scalar_parameter, "expected string or number or boolean"),
        (_wrong_strategy_kind, "is not one of"),
        (_empty_points, "at least 1 item"),
        (_empty_parameters, "at least 1 item"),
        (_blank_stability, "stability"),
        (_center_not_succeeded, "center"),
        (_missing_center_run, "jobId"),
    ],
)
def test_variation_evaluation_rejections(mutate, marker: str) -> None:
    payload = _variation_envelope()
    mutate(payload)

    errors = variation_payload_errors(payload)
    assert any(marker in error for error in errors), errors

    phase = build_hypothesis_development_workflow().spec.phase_specs["variation"]
    assert phase.validate is not None
    failure = phase.validate({"target": "forward_validation", "variation": payload})
    assert failure is not None and marker in failure

    with pytest.raises(WorkflowError):
        VariationEvaluation.from_dict(payload)


def test_variation_evaluation_rejects_foreign_target_and_bad_point_bounds() -> None:
    phase = build_hypothesis_development_workflow().spec.phase_specs["variation"]
    assert phase.validate is not None
    completion = {
        "target": "null_testing",
        "variation": _variation_envelope(),
    }
    assert validate_arguments(phase.completion_schema, completion)

    payload = _variation_envelope()
    points = payload["points"]
    assert isinstance(points, list)
    entry = points[0]
    assert isinstance(entry, dict)
    payload["points"] = [dict(entry) for _ in range(17)]
    assert any(
        "parameter vectors must be unique" in error
        for error in variation_payload_errors(payload)
    )
    with pytest.raises(WorkflowError):
        VariationEvaluation.from_dict(payload)


@pytest.mark.parametrize("target", ["forward_validation", "strategy_rca"])
def test_variation_fold_reconstructs_after_pure_rebuild(target: str) -> None:
    evaluation = VariationEvaluation.from_dict(_variation_envelope())
    transition = StoreTransition(
        sequence=1,
        from_phase="variation",
        to_phase=target,
        occurred_at=_moment(),
        intent_epoch=1,
        reason="variation accepted",
        completion={"target": target, "variation": evaluation.as_dict()},
    )
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-variation",
        phase=HypothesisDevelopmentPhase.VARIATION,
        intent_epoch=0,
    )

    folded = fold_transition(state, transition)

    assert folded.phase.value == target
    assert folded.variation == evaluation

    rebuilt = rebuild_state(
        WORKSPACE,
        "execution-variation",
        [transition],
        initial_phase=HypothesisDevelopmentPhase.VARIATION,
    )
    assert rebuilt.variation == evaluation


def test_outcome_emits_variation_and_resets_clear_it() -> None:
    evaluation = VariationEvaluation.from_dict(_variation_envelope())
    outcome = HypothesisDevelopmentOutcome(
        workspace_id=WORKSPACE,
        execution_id="execution-transport",
        phase=HypothesisDevelopmentPhase.VARIATION,
        fenced=False,
        transitions=(),
        variation=evaluation,
    )
    payload = outcome.as_dict()
    assert payload["variation"] == evaluation.as_dict()
    assert payload["nullTesting"] is None

    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-reset",
        phase=HypothesisDevelopmentPhase.VARIATION,
        intent_epoch=0,
        variation=evaluation,
    )
    for reset in (reset_premise, reset_discovery, reset_backtests):
        assert reset(state).variation is None


def test_variation_report_artifact_uses_the_frozen_ui_shape() -> None:
    evaluation = VariationEvaluation.from_dict(_variation_envelope())
    state = HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id="execution-artifact",
        phase=HypothesisDevelopmentPhase.VARIATION,
        intent_epoch=0,
        variation=evaluation,
    )
    artifact = variation_report_artifact(state)
    assert artifact is not None
    assert artifact["type"] == "variation"
    assert artifact["parameters"] == ["fast", "slow"]
    assert artifact["stability"] == evaluation.stability

    center = cast(dict[str, object], artifact["center"])
    assert set(center) == {
        "parameters",
        "parameterIdentity",
        "levelIdentities",
        "levelLabels",
        "jobId",
        "state",
        "metrics",
        "error",
    }
    assert center["state"] == "succeeded"

    points = cast(list[dict[str, object]], artifact["points"])
    assert len(points) == len(evaluation.points)
    assert all(
        set(point)
        == {
            "parameters",
            "parameterIdentity",
            "levelIdentities",
            "levelLabels",
            "jobId",
            "state",
            "metrics",
            "error",
        }
        for point in points
    )
    assert points[0]["metrics"] == {"netTotalReturn": 0.10, "profitFactor": 1.4}
    assert points[2]["metrics"] is None and points[2]["state"] == "failed"
    assert points[3]["metrics"] is None and points[3]["state"] == "running"

    assert (
        variation_report_artifact(
            HypothesisDevelopmentState(
                workspace_id=WORKSPACE,
                execution_id="execution-empty",
                phase=HypothesisDevelopmentPhase.VARIATION,
                intent_epoch=0,
            )
        )
        is None
    )
