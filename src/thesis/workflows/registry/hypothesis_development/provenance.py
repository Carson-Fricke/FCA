"""Provenance checks that bind every reported job to the run that executed it.

A completion that reports job-backed evidence is accepted only if each referenced
job is a retained tool job of this execution, was submitted in the phase that
owns it, ran the frozen strategy with the reported parameters, and has a
submission exposure record.  Forward validation additionally proves that every
job ran on the protected forward window under the frozen design, that no
submitted forward job is left unreported, and that the reported statistics are
the harness's own results.  These are facts checks, not operator approval: a
failed check rejects the completion with the mismatch named, and the agent
corrects it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from thesis.experiments.mandate import rfc3339_ns
from thesis.research.evidence import document_digest
from thesis.workflows.research_models import ProgramSnapshots
from thesis.workspaces.programs import parameter_assignment_sha256

from .contracts.strategy import program_reference
from .design import FrozenDesign, canonical_parameters, frozen_design
from .errors import WorkflowError
from .exposure import submitted_exposures
from .state import HypothesisDevelopmentState

if TYPE_CHECKING:
    from thesis.chat.service import ChatService
    from thesis.workflows.core.workflow import Workflow

    from .contracts.creation import HypothesisDevelopmentRequest

#: Phases whose completions reference job-backed evidence.
PROVENANCE_PHASES = frozenset(
    (
        "discovery",
        "optimization",
        "null_testing",
        "variation",
        "strategy_backtesting",
        "forward_validation",
    )
)

_HARNESS_STATES = {"queued": "running", "running": "running"}


JobResultSource = ProgramSnapshots


class _Malformed(Exception):
    """A completion field the provenance walk cannot read."""


@dataclass(frozen=True, slots=True)
class _Reference:
    """One job a completion reports, and what the report claims about it."""

    job_id: str
    owner: str
    role: str
    parameters: Mapping[str, object] | None = None
    seed: int | None = None
    state: str | None = None
    metrics: Mapping[str, object] | None = None


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _Malformed(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise _Malformed(f"{label} must be a list")
    return value


def _job_id(value: Mapping[str, object], label: str) -> str:
    job_id = value.get("jobId")
    if not isinstance(job_id, str) or not job_id.strip():
        raise _Malformed(f"{label} must name its jobId")
    return job_id


def _run_references(
    value: Mapping[str, object],
    owner: str,
    role: str,
    *,
    with_parameters: bool,
    with_seed: bool = False,
) -> _Reference:
    parameters = (
        canonical_parameters(_mapping(value.get("parameters"), "parameters"))
        if with_parameters
        else None
    )
    seed: int | None = None
    if with_seed:
        raw_seed = value.get("seed")
        if isinstance(raw_seed, bool) or not isinstance(raw_seed, int):
            raise _Malformed("replicate seed must be an integer")
        seed = raw_seed
    state = value.get("state")
    metrics = value.get("metrics")
    return _Reference(
        job_id=_job_id(value, role),
        owner=owner,
        role=role,
        parameters=parameters,
        seed=seed,
        state=state if isinstance(state, str) else None,
        metrics=metrics if isinstance(metrics, Mapping) else None,
    )


def _references(phase: str, completion: Mapping[str, object]) -> tuple[_Reference, ...]:
    """Return every job the phase's completion reports, with its owning phase."""
    if phase == "discovery":
        payload = _mapping(completion.get("discovery"), "discovery")
        return tuple(
            _Reference(
                job_id=_job_id(_mapping(item, "attempt"), "attempt"),
                owner="discovery",
                role="attempt",
            )
            for item in _sequence(payload.get("attempts"), "discovery.attempts")
        )
    if phase == "optimization":
        payload = _mapping(completion.get("optimization"), "optimization")
        return tuple(
            _run_references(
                _mapping(item, "assignment"),
                "optimization",
                "assignment",
                with_parameters=True,
            )
            for item in _sequence(payload.get("assignments"), "assignments")
        )
    if phase == "null_testing":
        payload = _mapping(completion.get("nullTesting"), "nullTesting")
        observed = _mapping(payload.get("observed"), "observed")
        references = [
            _Reference(
                job_id=_job_id(observed, "observed"),
                owner="optimization",
                role="observed",
                state="succeeded",
                metrics=_mapping(observed.get("metrics"), "observed.metrics"),
            )
        ]
        references.extend(
            _run_references(
                _mapping(item, "replicate"),
                "null_testing",
                "replicate",
                with_parameters=False,
                with_seed=True,
            )
            for item in _sequence(payload.get("replicates"), "replicates")
        )
        return tuple(references)
    if phase == "variation":
        payload = _mapping(completion.get("variation"), "variation")
        center = _mapping(payload.get("center"), "center")
        references = [
            _run_references(center, "optimization", "center", with_parameters=True)
        ]
        references.extend(
            _run_references(
                _mapping(item, "point"), "variation", "point", with_parameters=True
            )
            for item in _sequence(payload.get("points"), "points")
        )
        return tuple(references)
    if phase == "strategy_backtesting":
        payload = _mapping(completion.get("backtest"), "backtest")
        state = payload.get("state")
        return (
            _Reference(
                job_id=_job_id(payload, "backtest"),
                owner="strategy_backtesting",
                role="backtest",
                state=state if isinstance(state, str) else None,
            ),
        )
    if phase == "forward_validation":
        payload = _mapping(completion.get("forwardValidation"), "forwardValidation")
        observed = _mapping(payload.get("observed"), "observed")
        null = _mapping(payload.get("null"), "null")
        variation = _mapping(payload.get("variation"), "variation")
        references = [
            _Reference(
                job_id=_job_id(observed, "observed"),
                owner="forward_validation",
                role="observed",
                state="succeeded",
                metrics=_mapping(observed.get("metrics"), "observed.metrics"),
            ),
            _run_references(
                _mapping(variation.get("center"), "center"),
                "forward_validation",
                "center",
                with_parameters=True,
            ),
        ]
        references.extend(
            _run_references(
                _mapping(item, "replicate"),
                "forward_validation",
                "replicate",
                with_parameters=False,
                with_seed=True,
            )
            for item in _sequence(null.get("replicates"), "null.replicates")
        )
        references.extend(
            _run_references(
                _mapping(item, "point"),
                "forward_validation",
                "point",
                with_parameters=True,
            )
            for item in _sequence(variation.get("points"), "variation.points")
        )
        return tuple(references)
    raise _Malformed(f"phase {phase} carries no job references")


def _window_ns(start: object, end: object) -> tuple[int, int] | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return rfc3339_ns(start), rfc3339_ns(end)
    except (ValueError, WorkflowError):
        return None


def _metrics_match(
    reported: Mapping[str, object], harness: Mapping[str, object]
) -> bool:
    """Every reported metric must equal the harness's value for the same key."""
    for key, value in reported.items():
        if key not in harness:
            return False
        expected = harness[key]
        if isinstance(value, bool) or isinstance(expected, bool):
            if value != expected:
                return False
        elif isinstance(value, int | float | str) and isinstance(
            expected, int | float | str
        ):
            try:
                left, right = Decimal(str(value)), Decimal(str(expected))
            except InvalidOperation:
                if value != expected:
                    return False
            else:
                if not left.is_finite() or not right.is_finite() or left != right:
                    return False
        elif value != expected:
            return False
    return True


def _forward_design_errors(
    payload: Mapping[str, object],
    design: FrozenDesign,
    window_ns: tuple[int, int],
) -> list[str]:
    """Compare the reported forward design with the frozen development design."""
    errors: list[str] = []
    window = _mapping(payload.get("window"), "window")
    if _window_ns(window.get("start"), window.get("end")) != window_ns:
        errors.append("forward window is not the protected forward window")
    if payload.get("statistic") != design.statistic:
        errors.append("forward statistic differs from the frozen null design")
    if payload.get("direction") != design.direction:
        errors.append("forward direction differs from the frozen null design")
    null = _mapping(payload.get("null"), "null")
    if null.get("mode") != design.mode:
        errors.append("forward null mode differs from the frozen design")
    if null.get("blockSize") != design.block_size:
        errors.append("forward block size differs from the frozen null design")
    for key, expected in (
        ("signalAdequacy", design.signal_adequacy),
        ("evidenceScope", design.evidence_scope),
        ("failedReplicatePolicy", design.failed_replicate_policy),
        ("targetResolution", design.target_resolution),
    ):
        if null.get(key) != expected:
            errors.append(f"forward {key} differs from the frozen design")
    replicates = _sequence(null.get("replicates"), "null.replicates")
    seeds = sorted(
        seed
        for seed in (_mapping(item, "replicate").get("seed") for item in replicates)
        if isinstance(seed, int) and not isinstance(seed, bool)
    )
    if seeds != list(design.seeds):
        errors.append("forward null seeds differ from the frozen null design")
    variation = _mapping(payload.get("variation"), "variation")
    if variation.get("method") != design.variation_method:
        errors.append("forward variation method differs from the frozen design")
    if variation.get("interactions") != [
        list(group) for group in design.variation_interactions
    ]:
        errors.append("forward interactions differ from the frozen design")
    declared = _sequence(variation.get("parameters", []), "variation.parameters")
    names = sorted(name for name in declared if isinstance(name, str))
    if names != list(design.variation_parameters):
        errors.append("forward variation parameters differ from the frozen design")
    center = _mapping(variation.get("center"), "variation.center")
    if parameter_assignment_sha256(
        _mapping(center.get("parameters"), "center.parameters")
    ) != parameter_assignment_sha256(design.center):
        errors.append("forward center parameters differ from the frozen design")
    points = [
        _mapping(item.get("parameters"), "point.parameters")
        for item in _sequence(variation.get("points"), "variation.points")
        if isinstance(item, Mapping)
    ]
    if sorted(parameter_assignment_sha256(point) for point in points) != sorted(
        parameter_assignment_sha256(point) for point in design.variation_points
    ):
        errors.append("forward variation points differ from the frozen design")
    return errors


def _executed(chat: ChatService, workspace_id: str, job_id: str) -> bool:
    """Whether the job's dispatch happened or may have (not a never-started job)."""
    job = chat.find_tool_job(workspace_id, job_id)
    return job is None or job.submission_certainty != "not_started"


def _check_job_record(
    chat: ChatService,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    reference: _Reference,
    *,
    block_size: int | None,
    null_mode: str | None,
    forward: tuple[tuple[int, int], str] | None,
) -> str | None:
    """Verify one reported job against its retained tool record."""
    job = chat.find_tool_job(request.workspace_id, reference.job_id)
    if job is None:
        return f"job {reference.job_id} is not a retained job of this workflow"
    if job.execution_id != request.execution_id:
        return f"job {reference.job_id} belongs to another execution"
    attribution: Mapping[str, object] = job.attribution
    if attribution.get("phase") != reference.owner:
        return (
            f"job {reference.job_id} was submitted in phase "
            f"{attribution.get('phase')!r}, not {reference.owner!r}"
        )
    if reference.owner != "discovery":
        strategy = state.strategy or {}
        for key in ("programId", "kind", "version", "gitCommit"):
            if attribution.get(key) != strategy.get(key):
                return (
                    f"job {reference.job_id} ran {key} {attribution.get(key)!r}, "
                    "not the frozen strategy"
                )
    if reference.parameters is not None and attribution.get(
        "parameterAssignmentSha256"
    ) != parameter_assignment_sha256(reference.parameters):
        return f"job {reference.job_id} ran different parameters than reported"
    if reference.seed is not None:
        if not isinstance(attribution.get("nullPlanSha256"), str):
            return f"job {reference.job_id} has no null plan frozen before dispatch"
        if attribution.get("batchKind") != "null":
            return f"job {reference.job_id} is not a null replicate"
        if attribution.get("seed") != reference.seed:
            return f"job {reference.job_id} ran seed {attribution.get('seed')!r}"
        if attribution.get("blockSize") != block_size or (
            attribution.get("nullMode") != null_mode
        ):
            return f"job {reference.job_id} ran a different null design"
    if forward is not None:
        window_ns, design_sha = forward
        if (
            _window_ns(attribution.get("windowStart"), attribution.get("windowEnd"))
            != window_ns
        ):
            return f"job {reference.job_id} did not run on the forward window"
        if attribution.get("designSha256") != design_sha:
            return f"job {reference.job_id} was not submitted under the frozen design"
    return None


def _check_outcome(reference: _Reference, harness: Mapping[str, object]) -> str | None:
    """Require the reported state and metrics to be the harness's own result."""
    harness_state = harness.get("state")
    harness_state = _HARNESS_STATES.get(str(harness_state), harness_state)
    if reference.state == "succeeded":
        if harness_state != "succeeded":
            return f"job {reference.job_id} is not succeeded in the harness"
        if reference.metrics is None:
            return None
        harness_metrics = harness.get("metrics")
        if not isinstance(harness_metrics, Mapping) or not _metrics_match(
            reference.metrics, harness_metrics
        ):
            return f"job {reference.job_id} reports metrics the harness did not produce"
        return None
    if reference.state == "unknown":
        if harness_state == "succeeded":
            return f"job {reference.job_id} reports an unknown outcome that succeeded"
        return None
    if reference.state is not None and harness_state != reference.state:
        return (
            f"job {reference.job_id} is {harness_state!r} in the harness, "
            f"not {reference.state!r}"
        )
    return None


def _verify(
    *,
    chat: ChatService,
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    phase: str,
    phase_attempt_id: str | None,
    completion: Mapping[str, object],
    forward_window: tuple[str, str] | None,
    programs: JobResultSource | None,
) -> str | None:
    strategy = state.strategy
    if strategy is None and phase != "discovery":
        return "completion requires the strategy reference accepted by strategy design"
    references = _references(phase, completion)
    design: FrozenDesign | None = None
    forward: tuple[tuple[int, int], str] | None = None
    block_size: int | None = None
    null_mode: str | None = None
    if phase == "forward_validation":
        if forward_window is None:
            return "forward validation requires the protected forward window"
        window_ns = _window_ns(*forward_window)
        if window_ns is None:
            return "the protected forward window is malformed"
        design = frozen_design(state, *forward_window)
        references = tuple(
            _Reference(
                job_id=item.job_id,
                owner=item.owner,
                role=item.role,
                parameters=design.center
                if item.role == "observed"
                else item.parameters,
                seed=item.seed,
                state=item.state,
                metrics=item.metrics,
            )
            for item in references
        )
        forward = (window_ns, design.sha256())
        block_size = design.block_size
        null_mode = design.mode
        payload = _mapping(completion.get("forwardValidation"), "forwardValidation")
        for error in _forward_design_errors(payload, design, window_ns):
            return error
    elif phase == "null_testing":
        payload = _mapping(completion.get("nullTesting"), "nullTesting")
        declared_block = payload.get("blockSize")
        null_mode = str(payload.get("mode"))
        if isinstance(declared_block, int) and not isinstance(declared_block, bool):
            block_size = declared_block
        observed = next(item for item in references if item.role == "observed")
        candidates = state.candidate_evaluation
        if candidates is None or observed.job_id not in {
            item.job_id for item in candidates.shortlist
        }:
            return "null testing observation must be an accepted shortlisted candidate"
    elif phase == "variation" and state.null_testing is None:
        return "variation requires the accepted null testing"

    # The reported strategy must be the frozen reference for every phase that names one.
    named = {
        "optimization": ("optimization", None),
        "null_testing": ("nullTesting", None),
        "variation": ("variation", None),
        "forward_validation": ("forwardValidation", None),
        "strategy_backtesting": ("backtest", None),
    }.get(phase)
    if named is not None:
        payload = _mapping(completion.get(named[0]), named[0])
        try:
            reported = program_reference(payload.get("strategy"))
        except WorkflowError as error:
            return f"{phase} strategy reference is invalid: {error}"
        if reported != program_reference(strategy):
            return f"{phase} names a strategy other than the frozen reference"

    # Variation must center on the run null testing actually used.
    if phase == "variation" and state.null_testing is not None:
        center = next(item for item in references if item.role == "center")
        if center.job_id != state.null_testing.observed.job_id:
            return "variation centers on a run other than the null-testing observation"

    # Each job must be a retained tool job of this execution in its owning phase.
    # Exposure of the completing phase is scoped to this phase attempt: jobs from an
    # earlier attempt of the same phase (before a strategy revision) are not this
    # completion's evidence.  Reused optimization runs may come from any attempt.
    ledger = submitted_exposures(workflow, request.execution_id)
    exposed: dict[str, set[str]] = {}
    for entry in ledger:
        owner = str(entry.get("phase"))
        if owner == phase and entry.get("phaseAttemptId") != phase_attempt_id:
            continue
        if owner == "forward_validation" and forward is not None:
            if entry.get("designSha256") != forward[1]:
                continue
        exposed.setdefault(owner, set()).add(str(entry.get("jobId")))

    if phase in ("null_testing", "forward_validation"):
        seeds = sorted(item.seed for item in references if item.seed is not None)
        for reference in references:
            if reference.role != "replicate":
                continue
            entry = next(
                (item for item in ledger if item.get("jobId") == reference.job_id), {}
            )
            plan = entry.get("nullPlan")
            if not isinstance(plan, Mapping):
                return f"job {reference.job_id} has no null plan frozen before dispatch"
            if seeds != plan.get("seeds"):
                return (
                    "completion null seeds differ from the plan frozen before execution"
                )
            job = chat.find_tool_job(request.workspace_id, reference.job_id)
            if job is None or document_digest(plan) != job.attribution.get(
                "nullPlanSha256"
            ):
                return f"job {reference.job_id} does not match its frozen null plan"

    for reference in references:
        if reference.job_id not in exposed.get(reference.owner, set()):
            return (
                f"job {reference.job_id} has no submission record in {reference.owner}"
            )
        error = _check_job_record(
            chat,
            request,
            state,
            reference,
            block_size=block_size,
            null_mode=null_mode,
            forward=forward,
        )
        if error is not None:
            return error

    # Replicates and the forward center must share the observed run's configuration.
    anchors = [item for item in references if item.role in ("observed", "center")]
    for item in references:
        if item.role == "replicate" and anchors:
            observed = next((a for a in anchors if a.role == "observed"), None)
            if observed is not None:
                left = chat.find_tool_job(request.workspace_id, item.job_id)
                right = chat.find_tool_job(request.workspace_id, observed.job_id)
                if (
                    left is None
                    or right is None
                    or left.attribution.get("parameterAssignmentSha256")
                    != right.attribution.get("parameterAssignmentSha256")
                ):
                    return f"replicate {item.job_id} ran a different configuration than the observed run"

    # Membership: every executed job the phase submitted must be reported, and every
    # reported job of the phase must have actually been dispatched.
    owned = {item.job_id for item in references if item.owner == phase}
    submitted = exposed.get(phase, set())
    executed = {
        job_id for job_id in submitted if _executed(chat, request.workspace_id, job_id)
    }
    omitted = sorted(executed - owned)
    if omitted:
        return f"{phase} omits executed jobs: {', '.join(omitted)}"
    never_started = sorted(owned - executed)
    if never_started:
        return f"{phase} reports jobs that never started: {', '.join(never_started)}"

    # The harness receipt is authoritative for both development and forward evidence.
    if not references:
        return None
    if programs is None:
        return f"{phase} requires the harness job result port"
    for reference in references:
        try:
            harness = programs.job_result(reference.job_id)
        except Exception as error:  # noqa: BLE001 - unreadable result rejects
            return f"job {reference.job_id} has no readable harness result: {error}"
        if harness.get("job_id") != reference.job_id:
            return f"job {reference.job_id} returned a different job identity"
        error = _check_outcome(reference, harness)
        if error is not None:
            return error
        accepted = harness.get("accepted_input")
        job = chat.find_tool_job(request.workspace_id, reference.job_id)
        if not isinstance(accepted, Mapping) or job is None:
            return f"job {reference.job_id} has no immutable accepted input"
        if document_digest(accepted) != job.attribution.get("submittedInputSha256"):
            return f"job {reference.job_id} executed inputs other than its retained submission"
    return None


def verify_phase_completion(
    *,
    chat: ChatService,
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    phase: str,
    phase_attempt_id: str | None,
    completion: Mapping[str, object],
    forward_window: tuple[str, str] | None,
    programs: JobResultSource | None,
) -> str | None:
    """Return the first provenance mismatch a completion reports, or ``None``."""
    if phase not in PROVENANCE_PHASES:
        return None
    try:
        return _verify(
            chat=chat,
            workflow=workflow,
            request=request,
            state=state,
            phase=phase,
            phase_attempt_id=phase_attempt_id,
            completion=completion,
            forward_window=forward_window,
            programs=programs,
        )
    except _Malformed as error:
        return f"{phase} completion provenance is malformed: {error}"
    except WorkflowError as error:
        return str(error)


__all__ = [
    "PROVENANCE_PHASES",
    "JobResultSource",
    "verify_phase_completion",
]
