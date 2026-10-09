"""Durable experiment-ledger recording for the hypothesis-development workflow.

Every observed research attempt is appended to one append-only, hash-chained
artifact stored with the execution document under the ``"experiment_ledger"``
phase-result key.  The artifact holds the ordered canonical attempt documents;
recording reads the current document, keeps every attempt whose
content-addressed identity is already present, and writes the extended list
back with a revision compare-and-swap and one bounded retry on conflict.

The attempt identity is
``attempt-{execution_id}-{sha256(canonical(kind, description, payload))[:16]}``,
so re-recording the same observed attempt across retries and restarts is
idempotent while distinct attempts stay distinct.  ``observed_at`` is the
workflow clock at first recording time.

Exposure authority is the submission boundary.  Every job the workflow submits
is recorded here *before* it is dispatched, by the same durable step that
retains its tool job, so an attempt counts even if it later fails, is
abandoned, or its completion never arrives.  A completion never creates a
job-backed attempt; it references the submission records, and acceptance
rejects any reported job that has no submission record in its phase.  Only
attempts that are not job submissions (for example the hypothesis record) are
still derived from accepted completions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from thesis.experiments.ledger import AttemptKind, AttemptRecord, LedgerError
from thesis.persistence.journal import content_hash
from thesis.research.evidence import document_digest
from thesis.workflows.core.execution_store import (
    ExecutionDocument,
    ExecutionStoreConflict,
)
from thesis.workflows.core.workflow import Workflow

from .contracts.creation import HypothesisDevelopmentRequest
from .primitives import (
    _WORKFLOW_ACTOR,
    _WORKFLOW_ID,
    _WORKFLOW_VERSION,
    _iso,
)

_PHASE_RESULT_KEY = "experiment_ledger"
_WORKFLOW_CONTRACT = {"id": _WORKFLOW_ID, "version": _WORKFLOW_VERSION}
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_RECORDING_ATTEMPTS = 2

#: The attempt kind each job-submitting phase records at submission.  Forward
#: validation reads the protected holdout, so its looks are holdout looks.
_SUBMISSION_KIND: dict[str, AttemptKind] = {
    "discovery": AttemptKind.CANDIDATE,
    "falsification_trials": AttemptKind.METRIC,
    "strategy_backtesting": AttemptKind.VARIANT,
    "optimization": AttemptKind.PARAMETER,
    "null_testing": AttemptKind.NULL_TEST,
    "variation": AttemptKind.VARIATION,
    "forward_validation": AttemptKind.HOLDOUT_LOOK,
}


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise LedgerError("attempt payloads must be JSON-shaped")


def attempt_id(
    execution_id: str,
    kind: AttemptKind,
    description: str,
    payload: Mapping[str, object],
) -> str:
    """Return the content-addressed identity of one observed attempt."""
    if not isinstance(kind, AttemptKind):
        raise LedgerError("kind must be an AttemptKind")
    document = {
        "description": description,
        "kind": kind.value,
        "payload": _jsonable(dict(payload)),
    }
    try:
        canonical = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise LedgerError("attempt payloads must be canonical JSON") from error
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"attempt-{execution_id}-{digest[:16]}"


def attempt_record(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    kind: AttemptKind,
    description: str,
    payload: Mapping[str, object],
    *,
    contract_sha256: str,
) -> AttemptRecord:
    """Build one content-addressed attempt observed at the workflow clock."""
    return AttemptRecord(
        attempt_id=attempt_id(request.execution_id, kind, description, payload),
        contract_sha256=contract_sha256,
        kind=kind,
        description=description,
        actor=_WORKFLOW_ACTOR,
        observed_at=_iso(workflow.clock()),
        payload=dict(payload),
    )


def record_attempts(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    records: Sequence[AttemptRecord],
) -> None:
    """Append attempts to the execution's experiment ledger, once each."""
    store = getattr(workflow, "execution_store", None)
    if store is None or not records:
        return
    for attempt in range(_RECORDING_ATTEMPTS):
        document = store.get(request.execution_id)
        stored = _stored_attempts(document)
        known = {record.attempt_id for record in stored}
        additions: list[AttemptRecord] = []
        for record in records:
            if record.attempt_id in known:
                continue
            plan = record.payload.get("nullPlan")
            if isinstance(plan, Mapping):
                for prior in (*stored, *additions):
                    previous = prior.payload.get("nullPlan")
                    if (
                        isinstance(previous, Mapping)
                        and prior.payload.get("phase") == record.payload.get("phase")
                        and prior.payload.get("phaseAttemptId")
                        == record.payload.get("phaseAttemptId")
                        and document_digest(previous) != document_digest(plan)
                    ):
                        raise LedgerError(
                            "null seed plan or configuration changed after first submission"
                        )
            known.add(record.attempt_id)
            additions.append(record)
        if not additions:
            return
        artifact = {
            "attempts": [record.canonical_document for record in (*stored, *additions)]
        }
        try:
            store.put_phase_result(
                request.execution_id,
                _PHASE_RESULT_KEY,
                artifact,
                expected_revision=document.revision,
            )
            return
        except ExecutionStoreConflict:
            if attempt + 1 == _RECORDING_ATTEMPTS:
                raise


def record_submission_exposure(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    *,
    phase: str,
    job_id: str,
    tool_name: str,
    window_start: str | None,
    window_end: str | None,
    parameter_sha256: str | None,
    design_sha256: str | None = None,
    phase_attempt_id: str | None = None,
    null_plan: Mapping[str, object] | None = None,
) -> AttemptRecord:
    """Durably record one job submission as observed exposure, before dispatch.

    The record is the exposure authority for that job: its identity, phase,
    phase attempt, window, and configuration digest are what later completions
    must reference.  A store fault raises, so the caller never dispatches an
    unrecorded job.
    """
    kind = _SUBMISSION_KIND.get(phase)
    if kind is None:
        raise LedgerError(f"phase {phase!r} does not submit observed jobs")
    if getattr(workflow, "execution_store", None) is None:
        raise LedgerError("job submissions require the durable execution store")
    record = attempt_record(
        workflow,
        request,
        kind,
        f"{phase} job {job_id} submitted",
        {
            "designSha256": design_sha256,
            "jobId": job_id,
            "parameterAssignmentSha256": parameter_sha256,
            "phase": phase,
            "phaseAttemptId": phase_attempt_id,
            "nullPlan": None if null_plan is None else dict(null_plan),
            "toolName": tool_name,
            "windowEnd": window_end,
            "windowStart": window_start,
        },
        contract_sha256=_contract_sha256(workflow, request),
    )
    record_attempts(workflow, request, [record])
    return record


def submitted_exposures(
    workflow: Workflow[Any, Any, Any], execution_id: str
) -> tuple[Mapping[str, object], ...]:
    """Return the submission payloads recorded for one execution, in order."""
    store = getattr(workflow, "execution_store", None)
    if store is None:
        return ()
    document = store.get(execution_id)
    return tuple(
        dict(record.payload)
        for record in _stored_attempts(document)
        if "jobId" in record.payload and "phase" in record.payload
    )


def completion_attempts(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    phase: str,
    completion: Mapping[str, object],
) -> tuple[AttemptRecord, ...]:
    """Derive the non-job attempts an accepted completion contributes, if any."""
    builder = _BUILDERS.get(phase)
    if builder is None:
        return ()
    return builder(workflow, request, completion)


def _stored_attempts(document: ExecutionDocument) -> tuple[AttemptRecord, ...]:
    artifact = document.phase_results.get(_PHASE_RESULT_KEY)
    if artifact is None:
        return ()
    raw = artifact.get("attempts")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise LedgerError("the stored experiment ledger artifact is malformed")
    records: list[AttemptRecord] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise LedgerError("the stored experiment ledger artifact is malformed")
        records.append(AttemptRecord.from_dict(item))
    return tuple(records)


def _contract_sha256(
    workflow: Workflow[Any, Any, Any], request: HypothesisDevelopmentRequest
) -> str:
    reconstruct = getattr(workflow, "_reconstruct_state", None)
    if callable(reconstruct):
        hypothesis = getattr(reconstruct(request), "hypothesis", None)
        content = getattr(hypothesis, "content_sha256", None)
        if isinstance(content, str):
            return content
    return content_hash(dict(_WORKFLOW_CONTRACT))


def record_completion_attempts(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    phase: str,
    completion: Mapping[str, object],
) -> None:
    """Derive and persist the non-job attempts one accepted completion observed."""
    records = completion_attempts(workflow, request, phase, completion)
    if records:
        record_attempts(workflow, request, records)


def _describe(label: str, identity: object) -> str:
    if isinstance(identity, str) and _IDENTITY.fullmatch(identity):
        return f"{label} {identity} recorded"
    return f"{label} recorded"


def _model_creation_attempt(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    completion: Mapping[str, object],
) -> tuple[AttemptRecord, ...]:
    payload = completion.get("hypothesis")
    if not isinstance(payload, Mapping) or not payload:
        return ()
    model = payload.get("model")
    return (
        attempt_record(
            workflow,
            request,
            AttemptKind.HYPOTHESIS,
            _describe("model-creation hypothesis", payload.get("hypothesisId")),
            {
                "claim": payload.get("claim"),
                "hypothesisId": payload.get("hypothesisId"),
                "model": dict(model) if isinstance(model, Mapping) else None,
            },
            contract_sha256=_contract_sha256(workflow, request),
        ),
    )


_BUILDERS: dict[
    str,
    Callable[
        [Workflow[Any, Any, Any], HypothesisDevelopmentRequest, Mapping[str, object]],
        tuple[AttemptRecord, ...],
    ],
] = {
    "model_creation": _model_creation_attempt,
}

__all__ = [
    "attempt_id",
    "attempt_record",
    "completion_attempts",
    "record_attempts",
    "record_completion_attempts",
    "record_submission_exposure",
    "submitted_exposures",
]
