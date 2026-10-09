"""Research completion boundaries enforce declared methods and evidence limits."""

from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from thesis.experiments.mandate import derive_mandate
from thesis.workflows.registry.hypothesis_development.contracts.creation import (
    HypothesisDevelopmentRequest,
)
from thesis.workflows.registry.hypothesis_development.contracts.nulls import (
    NullTestEvaluation,
)
from thesis.workflows.registry.hypothesis_development.contracts.variation import (
    VariationEvaluation,
)
from thesis.workflows.registry.hypothesis_development.errors import WorkflowError
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_workflow,
)

from ._support import WORKSPACE, _kit, _null_test_envelope, _variation_envelope


def test_launch_freezes_explicit_partition_dependencies_and_rebuilds_them(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    thread = kit.chat.create_chat(
        WORKSPACE, title="Partition", owner_actor_id="operator"
    )
    trigger = kit.chat.create_message(
        WORKSPACE, thread.chat_id, actor_id="operator", body="Research"
    )
    with pytest.raises(WorkflowError, match="explicit partition"):
        kit.workflow.launch(
            WORKSPACE,
            thread.chat_id,
            actor_id="operator",
            ideas="Research",
            trigger_message_id=trigger.message_id,
        )
    assert kit.chat.list_executions(WORKSPACE) == ()
    declaration = {
        "historyDays": 500,
        "forwardDays": 120,
        "informationHorizonDays": 4,
        "positionHorizonDays": 7,
        "liquidationDays": 2,
    }
    request = kit.workflow.launch(
        WORKSPACE,
        thread.chat_id,
        actor_id="operator",
        ideas="Research",
        trigger_message_id=trigger.message_id,
        launch_options={"partition": declaration},
    )
    durable = kit.execution_store.get(request.execution_id).launch_request
    partition = cast(dict[str, object], durable["partition"])
    assert partition["purgeDays"] == 7
    assert partition["embargoDays"] == 2
    declaration["forwardDays"] = 20
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    rebuilt = HypothesisDevelopmentRequest.from_execution(kit.chat, execution, durable)
    assert rebuilt.as_dict()["partition"] == durable["partition"]
    assert derive_mandate(WORKSPACE, request.execution_id, durable).forward_days == 120


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
    phase = build_hypothesis_development_workflow().spec.phase_specs["null_testing"]
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
    phase = build_hypothesis_development_workflow().spec.phase_specs["variation"]
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
