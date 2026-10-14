"""Research completion boundaries enforce declared methods and evidence limits."""

from copy import deepcopy
from typing import cast

import pytest

from thesis.extensions.workflows.hypothesis_development.contracts.nulls import (
    NullTestEvaluation,
)
from thesis.extensions.workflows.hypothesis_development.contracts.variation import (
    VariationEvaluation,
)
from thesis.extensions.workflows.hypothesis_development.errors import WorkflowError
from thesis.extensions.workflows.hypothesis_development.spec import (
    phase_declarations,
)

from ._support import (
    _candidate_evaluation_envelope,
    _forward_envelope,
    _null_test_envelope,
    _variation_envelope,
)


def test_completion_contracts_reject_missing_required_evidence() -> None:
    phases = {item.spec.phase: item.spec for item in phase_declarations()}
    candidate = _candidate_evaluation_envelope()
    variation = _variation_envelope()
    forward = _forward_envelope()
    for phase_name, target, key, payload, evidence, field in (
        (
            "optimization",
            "null_testing",
            "optimization",
            candidate,
            candidate,
            "batchId",
        ),
        (
            "variation",
            "forward_validation",
            "variation",
            variation,
            variation,
            "batchId",
        ),
        (
            "forward_validation",
            "completed",
            "forwardValidation",
            forward,
            cast(dict[str, object], forward["observed"]),
            "jobId",
        ),
    ):
        validate = phases[phase_name].validate
        assert validate is not None
        assert validate({"target": target, key: payload}) is None
        del evidence[field]
        assert validate({"target": target, key: payload}) is not None


def test_null_completion_supports_large_frozen_seed_plan_and_conservative_failures() -> (
    None
):
    payload = _null_test_envelope()
    original_replicates = cast(list[dict[str, object]], payload["replicates"])
    prototype: dict[str, object] = {
        **original_replicates[0],
        "metrics": {"sharpe": 0.1},
    }
    replicates: list[dict[str, object]] = [
        dict(prototype, seed=seed, jobId=f"job-null-{seed}") for seed in range(999)
    ]
    payload["replicates"] = replicates
    replicates[0].update(state="failed", metrics=None, error="execution failed")
    payload.update(exceedanceCount=0, pValue=2 / 1000, targetResolution=0.001)
    phase = next(
        item.spec for item in phase_declarations() if item.spec.phase == "null_testing"
    )
    assert phase.validate is not None
    assert phase.validate({"target": "variation", "nullTesting": payload}) is None
    evaluation = NullTestEvaluation.from_dict(payload)
    assert len(evaluation.replicates) == 999
    assert evaluation.p_value == 0.002
    payload["pValue"] = 1 / 999
    assert phase.validate({"target": "variation", "nullTesting": payload}) is not None


def test_null_method_dependency_and_resolution_are_enforced() -> None:
    payload = _null_test_envelope()
    payload.update(mode="permutation", blockSize=1)
    adequacy = cast(dict[str, object], payload["signalAdequacy"])
    adequacy.update(
        destroyedDependency="serial_order", preservedProperties=["marginal_returns"]
    )
    assert NullTestEvaluation.from_dict(payload).mode == "permutation"
    adequacy["preservedProperties"] = ["within_block_dependence"]
    with pytest.raises(WorkflowError, match="within_block_dependence"):
        NullTestEvaluation.from_dict(payload)
    adequacy["preservedProperties"] = ["marginal_returns"]
    payload["targetResolution"] = 0.001
    with pytest.raises(WorkflowError, match="targetResolution"):
        NullTestEvaluation.from_dict(payload)
    payload["targetResolution"] = 0.25
    payload["evidenceScope"] = "search_adjusted"
    with pytest.raises(WorkflowError):
        NullTestEvaluation.from_dict(payload)


def test_variation_completion_supports_joint_experiments_and_more_than_sixteen_points() -> (
    None
):
    payload = _variation_envelope()
    payload.update(method="joint", interactions=[["fast", "slow"]])
    prototype = cast(list[dict[str, object]], payload["points"])[0]
    center_record = cast(dict[str, object], payload["center"])
    center = cast(dict[str, object], center_record["parameters"])
    points: list[dict[str, object]] = [
        dict(
            prototype,
            jobId=f"job-variation-{index}",
            parameters={**center, "fast": index + 100},
        )
        for index in range(20)
    ]
    points.append(
        dict(
            prototype,
            jobId="job-joint",
            parameters={**center, "fast": 101, "slow": 201},
        )
    )
    payload["points"] = points
    phase = next(
        item.spec for item in phase_declarations() if item.spec.phase == "variation"
    )
    assert phase.validate is not None
    assert (
        phase.validate({"target": "forward_validation", "variation": payload}) is None
    )
    result = VariationEvaluation.from_dict(payload)
    assert result.method == "joint"
    assert result.interactions == (("fast", "slow"),)
    undeclared = deepcopy(payload)
    undeclared.update(method="one_at_a_time", interactions=[])
    assert (
        phase.validate({"target": "forward_validation", "variation": undeclared})
        is not None
    )
