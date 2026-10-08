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

from thesis.workflows.registry.hypothesis_development.contracts.nulls import (
    NullTestEvaluation,
)
from thesis.workflows.registry.hypothesis_development.contracts.strategy import (
    program_reference,
)
from thesis.workflows.registry.hypothesis_development.contracts.variation import (
    VariationEvaluation,
)
from thesis.workflows.registry.hypothesis_development.design import frozen_design
from thesis.workflows.registry.hypothesis_development.exposure import (
    record_submission_exposure,
)
from thesis.workflows.registry.hypothesis_development.primitives import (
    _WORKFLOW_ACTOR,
)
from thesis.workflows.registry.hypothesis_development.provenance import (
    verify_phase_completion,
)
from thesis.workspaces.programs import parameter_assignment_sha256

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
            "pValue": 2 / 3,
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
        attribution=dict(attribution),
    )
    record_submission_exposure(
        kit.workflow,
        request,
        phase=phase,
        job_id=job_id,
        tool_name="run_backtest",
        window_start=str(attribution.get("windowStart")),
        window_end=str(attribution.get("windowEnd")),
        parameter_sha256=str(attribution.get("parameterAssignmentSha256")),
        design_sha256=design_sha,
        phase_attempt_id=ATTEMPT,
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
        kit.workflow._reconstruct_state(request),
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
                    "seed": job["seed"],
                    "blockSize": 7,
                    "nullMode": "block_permutation",
                }
            )
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
        workflow=kit.workflow,
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
        kit.workflow,
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
        "job_id": "fwd-observed",
        "state": "succeeded",
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
        },
        design_sha="",
    )


def test_discovery_studies_are_bound_before_any_strategy_exists(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    _discovery_study(kit, request, "study-1")
    state = kit.workflow._reconstruct_state(request)
    assert state.strategy is None
    error = verify_phase_completion(
        chat=kit.chat,
        workflow=kit.workflow,
        request=request,
        state=state,
        phase="discovery",
        phase_attempt_id=ATTEMPT,
        completion={"discovery": {"attempts": [{"jobId": "study-1"}]}},
        forward_window=None,
        programs=None,
    )
    assert error is None


def test_discovery_omitting_an_executed_study_is_rejected(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    _discovery_study(kit, request, "study-1")
    state = kit.workflow._reconstruct_state(request)
    error = verify_phase_completion(
        chat=kit.chat,
        workflow=kit.workflow,
        request=request,
        state=state,
        phase="discovery",
        phase_attempt_id=ATTEMPT,
        completion={"discovery": {"attempts": []}},
        forward_window=None,
        programs=None,
    )
    assert error is not None and "omits executed jobs" in error
