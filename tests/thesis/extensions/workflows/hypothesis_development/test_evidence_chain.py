"""Forward validation accepts only evidence that provably came from the frozen design.

The fixture submits every forward job the way the program boundary does: a tool
job with its attribution, then its submission exposure, under the frozen design
digest.  The verifier then checks the completion against those records and the
harness's own results.  Each rejection changes one fact the exit criterion names.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from thesis.extensions.workflows.hypothesis_development.contracts.nulls import (
    NullTestEvaluation,
)
from thesis.extensions.workflows.hypothesis_development.contracts.strategy import (
    program_reference,
)
from thesis.extensions.workflows.hypothesis_development.contracts.variation import (
    VariationEvaluation,
)
from thesis.extensions.workflows.hypothesis_development.design import frozen_design
from thesis.extensions.workflows.hypothesis_development.endpoint import (
    HypothesisExtension,
)
from thesis.extensions.workflows.hypothesis_development.errors import WorkflowError
from thesis.extensions.workflows.hypothesis_development.exposure import (
    record_submission_exposure,
)
from thesis.extensions.workflows.hypothesis_development.primitives import (
    _WORKFLOW_ACTOR,
)
from thesis.extensions.workflows.hypothesis_development.provenance import (
    verify_phase_completion,
)
from thesis.extensions.workflows.hypothesis_development.session_services import (
    HypothesisSessionServices,
)
from thesis.research.evidence import document_digest
from thesis.workspaces.programs import parameter_assignment_sha256
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.subagents.store import PersistenceSubAgentStore

from ._support import (
    WORKSPACE,
    FakePrograms,
    _Kit,
    _kit,
    _null_test_envelope,
    _request,
    _variation_envelope,
)

WINDOW = ("2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z")
DEVELOPMENT_WINDOW = ("2025-01-01T00:00:00Z", "2026-03-01T00:00:00Z")
ATTEMPT = "attempt-1"
CENTER = {"fast": 5, "slow": 20}
POINTS = (
    {"fast": 4, "slow": 20},
    {"fast": 6, "slow": 20},
    {"fast": 5, "slow": 19},
    {"fast": 5, "slow": 21},
)


def _hash(parameters: Mapping[str, object]) -> str:
    return parameter_assignment_sha256(parameters)


def _forward_payload() -> dict[str, Any]:
    """A forward completion whose every reported fact is true of its jobs."""
    strategy = cast(Mapping[str, object], _null_test_envelope()["strategy"])
    return {
        "strategy": dict(strategy),
        "window": {"start": WINDOW[0], "end": WINDOW[1]},
        "statistic": "sharpe",
        "direction": "upper",
        "observed": {"jobId": "fwd-observed", "metrics": {"sharpe": 1.0}, "value": 1.0},
        "null": {
            "mode": "block_permutation",
            "signalAdequacy": {
                "signal": "lagged price relationship",
                "destroyedDependency": "inter_block_order",
                "preservedProperties": ["marginal_returns", "within_block_dependence"],
                "limitations": "Within-block dependence remains; calendar volatility schedule is not preserved.",
            },
            "evidenceScope": "selection_conditional",
            "failedReplicatePolicy": "conservative_exceedance",
            "targetResolution": 0.25,
            "batchId": "forward-null-0123456789ab",
            "blockSize": 7,
            "replicates": [
                {
                    "seed": 1,
                    "jobId": "fwd-null-1",
                    "state": "succeeded",
                    "metrics": {"sharpe": 1.2},
                    "error": None,
                },
                {
                    "seed": 2,
                    "jobId": "fwd-null-2",
                    "state": "succeeded",
                    "metrics": {"sharpe": 0.5},
                    "error": None,
                },
                {
                    "seed": 3,
                    "jobId": "fwd-null-3",
                    "state": "failed",
                    "metrics": None,
                    "error": "run terminated before completing",
                },
            ],
            "exceedanceCount": 1,
            "pValue": 3 / 4,
        },
        "variation": {
            "batchId": "forward-variation-0123456789ab",
            "center": {
                "parameters": dict(CENTER),
                "jobId": "fwd-center",
                "state": "succeeded",
                "metrics": {"sharpe": 1.0},
                "error": None,
            },
            "points": [
                {
                    "parameters": dict(POINTS[0]),
                    "jobId": "fwd-var-1",
                    "state": "succeeded",
                    "metrics": {"sharpe": 0.9},
                    "error": None,
                },
                {
                    "parameters": dict(POINTS[1]),
                    "jobId": "fwd-var-2",
                    "state": "succeeded",
                    "metrics": {"sharpe": 1.1},
                    "error": None,
                },
                {
                    "parameters": dict(POINTS[2]),
                    "jobId": "fwd-var-3",
                    "state": "failed",
                    "metrics": None,
                    "error": "run terminated before completing",
                },
                {
                    "parameters": dict(POINTS[3]),
                    "jobId": "fwd-var-4",
                    "state": "succeeded",
                    "metrics": {"sharpe": 0.95},
                    "error": None,
                },
            ],
            "parameters": ["fast", "slow"],
            "method": "one_at_a_time",
            "interactions": [],
            "stability": "the neighborhood holds",
        },
        "comparison": "development sharpe 1.0 against forward sharpe 1.0",
        "limitations": None,
    }


def _forward_jobs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every forward job the payload reports, with the configuration it ran."""
    variation = payload["variation"]
    jobs: list[dict[str, Any]] = [
        {
            "jobId": payload["observed"]["jobId"],
            "params": CENTER,
            "state": "succeeded",
            "metrics": payload["observed"]["metrics"],
            "seed": None,
        }
    ]
    for item in payload["null"]["replicates"]:
        jobs.append(
            {
                "jobId": item["jobId"],
                "params": CENTER,
                "state": item["state"],
                "metrics": item["metrics"],
                "seed": item["seed"],
            }
        )
    jobs.append(
        {
            "jobId": variation["center"]["jobId"],
            "params": CENTER,
            "state": "succeeded",
            "metrics": variation["center"]["metrics"],
            "seed": None,
        }
    )
    for item in variation["points"]:
        jobs.append(
            {
                "jobId": item["jobId"],
                "params": item["parameters"],
                "state": item["state"],
                "metrics": item["metrics"],
                "seed": None,
            }
        )
    return jobs


def _submit(
    kit: _Kit,
    request: Any,
    job_id: str,
    *,
    phase: str,
    attribution: Mapping[str, object],
    design_sha: str,
) -> None:
    """Submit one job as the program boundary does: tool job, exposure, then start."""
    kit.chat.submit_tool_job(
        WORKSPACE,
        request.execution_id,
        tool_name="run_backtest",
        actor_id=_WORKFLOW_ACTOR,
        job_id=job_id,
        attribution={
            **{key: value for key, value in attribution.items() if key != "nullPlan"},
            **(
                {
                    "nullPlanSha256": document_digest(
                        cast(Mapping[str, object], attribution["nullPlan"])
                    )
                }
                if isinstance(attribution.get("nullPlan"), Mapping)
                else {}
            ),
        },
    )
    record_submission_exposure(
        kit.extension.activities(request),
        request,
        phase=phase,
        job_id=job_id,
        tool_name="run_backtest",
        window_start=str(attribution.get("windowStart")),
        window_end=str(attribution.get("windowEnd")),
        parameter_sha256=str(attribution.get("parameterAssignmentSha256")),
        design_sha256=design_sha,
        phase_attempt_id=ATTEMPT,
        null_plan=cast(Mapping[str, object] | None, attribution.get("nullPlan")),
    )
    kit.chat.start_tool_job(WORKSPACE, job_id, actor_id=_WORKFLOW_ACTOR)


def _fixture(
    tmp_path: Path,
    *,
    attribution_for: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[_Kit, Any, Any, FakePrograms, dict[str, Any], str]:
    """Accept the development state and submit every forward job it reports."""
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    strategy = program_reference(_null_test_envelope()["strategy"])
    state = replace(
        kit.extension.state(
            request, kit.execution_store.get((request).execution_id).transitions
        ),
        strategy=strategy,
        null_testing=NullTestEvaluation.from_dict(_null_test_envelope()),
        variation=VariationEvaluation.from_dict(_variation_envelope()),
    )
    payload = _forward_payload()
    design_sha = frozen_design(state, *WINDOW).sha256()
    programs = FakePrograms()
    for job in _forward_jobs(payload):
        attribution: dict[str, Any] = {
            "programId": strategy["programId"],
            "kind": strategy["kind"],
            "version": strategy["version"],
            "gitCommit": strategy["gitCommit"],
            "phase": "forward_validation",
            "windowStart": WINDOW[0],
            "windowEnd": WINDOW[1],
            "designSha256": design_sha,
            "parameterAssignmentSha256": _hash(job["params"]),
        }
        if job["seed"] is not None:
            attribution.update(
                {
                    "batchKind": "null",
                    "nullPlan": {
                        "mode": "block_permutation",
                        "blockSize": 7,
                        "seeds": [1, 2, 3],
                        "parameters": dict(CENTER),
                    },
                    "seed": job["seed"],
                    "blockSize": 7,
                    "nullMode": "block_permutation",
                }
            )
        accepted_input = {
            "job_id": job["jobId"],
            "parameters": job["params"],
            "start": WINDOW[0],
            "end": WINDOW[1],
            "seed": job["seed"],
        }
        attribution["submittedInputSha256"] = document_digest(accepted_input)
        if attribution_for is not None:
            attribution = attribution_for(job["jobId"], attribution)
        _submit(
            kit,
            request,
            job["jobId"],
            phase="forward_validation",
            attribution=attribution,
            design_sha=design_sha,
        )
        programs.results[job["jobId"]] = {
            "job_id": job["jobId"],
            "state": job["state"],
            "accepted_input": accepted_input,
            "metrics": dict(job["metrics"] or {}),
        }
    return kit, request, state, programs, payload, design_sha


def _verify(
    kit: _Kit,
    request: Any,
    state: Any,
    programs: FakePrograms,
    payload: Mapping[str, Any],
    *,
    window: tuple[str, str] = WINDOW,
) -> str | None:
    return verify_phase_completion(
        chat=kit.chat,
        workflow=kit.extension.activities(request),
        request=request,
        state=state,
        phase="forward_validation",
        phase_attempt_id=ATTEMPT,
        completion={"forwardValidation": payload},
        forward_window=window,
        programs=programs,
    )


def test_a_forward_result_true_of_its_jobs_is_accepted(tmp_path: Path) -> None:
    kit, request, state, programs, payload, _ = _fixture(tmp_path)
    assert _verify(kit, request, state, programs, payload) is None


def test_forward_jobs_run_on_the_development_window_are_rejected(
    tmp_path: Path,
) -> None:
    def development_window(job_id: str, attribution: dict[str, Any]) -> dict[str, Any]:
        if job_id != "fwd-var-1":
            return attribution
        return {
            **attribution,
            "windowStart": DEVELOPMENT_WINDOW[0],
            "windowEnd": DEVELOPMENT_WINDOW[1],
        }

    kit, request, state, programs, payload, _ = _fixture(
        tmp_path, attribution_for=development_window
    )
    error = _verify(kit, request, state, programs, payload)
    assert error is not None and "forward window" in error


def test_a_point_run_with_altered_parameters_is_rejected(tmp_path: Path) -> None:
    def altered(job_id: str, attribution: dict[str, Any]) -> dict[str, Any]:
        if job_id != "fwd-var-2":
            return attribution
        return {
            **attribution,
            "parameterAssignmentSha256": _hash({"fast": 9, "slow": 20}),
        }

    kit, request, state, programs, payload, _ = _fixture(
        tmp_path, attribution_for=altered
    )
    error = _verify(kit, request, state, programs, payload)
    assert error is not None and "different parameters" in error


def test_a_reported_job_from_another_phase_is_rejected(tmp_path: Path) -> None:
    kit, request, state, programs, payload, _ = _fixture(tmp_path)
    record_submission_exposure(
        kit.extension.activities(request),
        request,
        phase="optimization",
        job_id="opt-run-1",
        tool_name="run_backtest",
        window_start=None,
        window_end=None,
        parameter_sha256=_hash(POINTS[0]),
    )
    programs.results["opt-run-1"] = {
        "job_id": "opt-run-1",
        "state": "succeeded",
        "metrics": {"sharpe": 0.9},
    }
    borrowed = dict(payload)
    variation = dict(payload["variation"])
    points = [dict(point) for point in variation["points"]]
    points[0] = {**points[0], "jobId": "opt-run-1"}
    variation["points"] = points
    borrowed["variation"] = variation
    error = _verify(kit, request, state, programs, borrowed)
    assert error is not None and "no submission record in forward_validation" in error


def test_an_executed_forward_job_left_unreported_is_rejected(tmp_path: Path) -> None:
    kit, request, state, programs, payload, design_sha = _fixture(tmp_path)
    strategy = program_reference(_null_test_envelope()["strategy"])
    hidden = {
        "programId": strategy["programId"],
        "kind": strategy["kind"],
        "version": strategy["version"],
        "gitCommit": strategy["gitCommit"],
        "phase": "forward_validation",
        "windowStart": WINDOW[0],
        "windowEnd": WINDOW[1],
        "designSha256": design_sha,
        "parameterAssignmentSha256": _hash({"fast": 7, "slow": 20}),
    }
    _submit(
        kit,
        request,
        "fwd-hidden",
        phase="forward_validation",
        attribution=hidden,
        design_sha=design_sha,
    )
    programs.results["fwd-hidden"] = {
        "job_id": "fwd-hidden",
        "state": "succeeded",
        "metrics": {"sharpe": 0.4},
    }
    error = _verify(kit, request, state, programs, payload)
    assert error is not None and "omits executed jobs" in error


def test_a_reported_metric_the_harness_did_not_produce_is_rejected(
    tmp_path: Path,
) -> None:
    kit, request, state, programs, payload, _ = _fixture(tmp_path)
    programs.results["fwd-observed"] = {
        **programs.results["fwd-observed"],
        "metrics": {"sharpe": 9.9},
    }
    error = _verify(kit, request, state, programs, payload)
    assert error is not None and "metrics the harness did not produce" in error


def test_a_forward_strategy_other_than_the_frozen_one_is_rejected(
    tmp_path: Path,
) -> None:
    kit, request, state, programs, payload, _ = _fixture(tmp_path)
    drifted = dict(payload)
    drifted["strategy"] = {**payload["strategy"], "programId": "other"}
    error = _verify(kit, request, state, programs, drifted)
    assert error is not None and "frozen reference" in error


def test_a_forward_window_other_than_the_protected_one_is_rejected(
    tmp_path: Path,
) -> None:
    kit, request, state, programs, payload, _ = _fixture(tmp_path)
    error = _verify(kit, request, state, programs, payload, window=DEVELOPMENT_WINDOW)
    assert error is not None and "protected forward window" in error


def _discovery_study(kit: _Kit, request: Any, job_id: str) -> None:
    _submit(
        kit,
        request,
        job_id,
        phase="discovery",
        attribution={
            "programId": "momentum-study",
            "kind": "study",
            "version": "c" * 64,
            "gitCommit": "d" * 40,
            "phase": "discovery",
            "parameterAssignmentSha256": _hash({"lookback": 10}),
            "submittedInputSha256": document_digest({"job_id": job_id}),
        },
        design_sha="",
    )


def test_discovery_studies_are_bound_before_any_strategy_exists(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    _discovery_study(kit, request, "study-1")
    programs = FakePrograms()
    programs.results["study-1"] = {
        "job_id": "study-1",
        "state": "succeeded",
        "accepted_input": {"job_id": "study-1"},
        "metrics": {},
    }
    state = kit.extension.state(
        request, kit.execution_store.get((request).execution_id).transitions
    )
    assert state.strategy is None
    error = verify_phase_completion(
        chat=kit.chat,
        workflow=kit.extension.activities(request),
        request=request,
        state=state,
        phase="discovery",
        phase_attempt_id=ATTEMPT,
        completion={"discovery": {"attempts": [{"jobId": "study-1"}]}},
        forward_window=None,
        programs=programs,
    )
    assert error is None


def test_discovery_omitting_an_executed_study_is_rejected(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    _discovery_study(kit, request, "study-1")
    state = kit.extension.state(
        request, kit.execution_store.get((request).execution_id).transitions
    )
    error = verify_phase_completion(
        chat=kit.chat,
        workflow=kit.extension.activities(request),
        request=request,
        state=state,
        phase="discovery",
        phase_attempt_id=ATTEMPT,
        completion={"discovery": {"attempts": []}},
        forward_window=None,
        programs=None,
    )
    assert error is not None and "omits executed jobs" in error


def test_accepted_inputs_cannot_be_changed_after_retention(tmp_path: Path) -> None:
    kit, request, state, programs, payload, _ = _fixture(tmp_path)
    programs.results["fwd-observed"] = {
        **programs.results["fwd-observed"],
        "accepted_input": {"start": DEVELOPMENT_WINDOW[0]},
    }
    error = _verify(kit, request, state, programs, payload)
    assert error is not None and "executed inputs" in error


def test_decimal_harness_statistics_are_accepted_without_float_reconstruction(
    tmp_path: Path,
) -> None:
    kit, request, state, programs, payload, _ = _fixture(tmp_path)
    programs.results["fwd-observed"] = {
        **programs.results["fwd-observed"],
        "metrics": {"sharpe": "1.000000000000000000"},
    }
    assert _verify(kit, request, state, programs, payload) is None


def test_another_jobs_result_is_rejected(tmp_path: Path) -> None:
    kit, request, state, programs, payload, _ = _fixture(tmp_path)
    programs.results["fwd-observed"] = {
        **programs.results["fwd-observed"],
        "job_id": "another-job",
    }
    error = _verify(kit, request, state, programs, payload)
    assert error is not None and "job identity" in error


def test_null_seed_plan_is_frozen_before_dispatch(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    service = HypothesisSessionServices(
        chat=kit.chat,
        store=PersistenceSubAgentStore(kit.chat.persistence),
        extension=cast(HypothesisExtension, (kit.workflow).extension),
        workflow=lambda: kit.workflow,
        harness_tools=None,
        programs=FakePrograms(),
        activity=lambda context, tool, arguments, execute: execute(),
    )
    context = ToolContext(
        workspace_id=WORKSPACE,
        chat_id=request.chat_id,
        conversation_id=request.chat_id,
        actor_id=_WORKFLOW_ACTOR,
        workflow_execution_id=request.execution_id,
        phase_attempt_id=ATTEMPT,
    )
    retain = service._program_job_retention("null_testing")
    attribution = {
        "nullPlan": {
            "mode": "permutation",
            "blockSize": 1,
            "seeds": [1, 2],
            "parameters": dict(CENTER),
        }
    }
    retain(context, "null-first", "run_backtest", attribution)
    changed = {
        "nullPlan": {
            "mode": "permutation",
            "blockSize": 1,
            "seeds": [1, 2, 3],
            "parameters": dict(CENTER),
        }
    }
    with pytest.raises(WorkflowError, match="changed after first submission"):
        retain(context, "null-adaptive", "run_backtest", changed)
    assert kit.chat.find_tool_job(WORKSPACE, "null-adaptive") is None


def test_development_statistics_are_checked_against_the_harness(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    strategy = program_reference(_null_test_envelope()["strategy"])
    state = replace(
        kit.extension.state(
            request, kit.execution_store.get((request).execution_id).transitions
        ),
        strategy=strategy,
    )
    accepted_input = {"job_id": "candidate-1", "parameters": dict(CENTER)}
    _submit(
        kit,
        request,
        "candidate-1",
        phase="optimization",
        attribution={
            **strategy,
            "phase": "optimization",
            "parameterAssignmentSha256": _hash(CENTER),
            "submittedInputSha256": document_digest(accepted_input),
        },
        design_sha="",
    )
    programs = FakePrograms()
    programs.results["candidate-1"] = {
        "job_id": "candidate-1",
        "state": "succeeded",
        "metrics": {"net_pnl": "12"},
        "accepted_input": accepted_input,
    }
    completion = {
        "optimization": {
            "strategy": strategy,
            "assignments": [
                {
                    "jobId": "candidate-1",
                    "parameters": dict(CENTER),
                    "state": "succeeded",
                    "metrics": {"net_pnl": "120"},
                }
            ],
        }
    }
    error = verify_phase_completion(
        chat=kit.chat,
        workflow=kit.extension.activities(request),
        request=request,
        state=state,
        phase="optimization",
        phase_attempt_id=ATTEMPT,
        completion=completion,
        forward_window=None,
        programs=programs,
    )
    assert error is not None and "metrics the harness did not produce" in error
