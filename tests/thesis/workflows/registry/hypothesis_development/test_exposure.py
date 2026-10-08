"""Durable experiment-ledger exposure, recorded at the job submission boundary.

Every submitted job is an observed look at data.  Its exposure record is written
before dispatch, so a failed, abandoned, or never-reported attempt still counts.
Completions reference those records; they never create job-backed attempts of
their own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from thesis.experiments.ledger import (
    AttemptKind,
    AttemptLedger,
    AttemptRecord,
    LedgerError,
)
from thesis.workflows.core.execution_store import (
    ExecutionDocument,
    ExecutionStoreConflict,
    InMemoryExecutionStore,
)
from thesis.workflows.registry.hypothesis_development.exposure import (
    record_completion_attempts,
    record_submission_exposure,
)

from ._support import (
    WORKSPACE,
    _candidate_evaluation_envelope,
    _discovery_envelope,
    _forward_envelope,
    _hypothesis_envelope,
    _Kit,
    _kit,
    _null_test_envelope,
    _request,
    _variation_envelope,
)

SUBMISSION_KINDS = {
    "discovery": AttemptKind.CANDIDATE,
    "falsification_trials": AttemptKind.METRIC,
    "strategy_backtesting": AttemptKind.VARIANT,
    "optimization": AttemptKind.PARAMETER,
    "null_testing": AttemptKind.NULL_TEST,
    "variation": AttemptKind.VARIATION,
    "forward_validation": AttemptKind.HOLDOUT_LOOK,
}


class _ConflictingStore(InMemoryExecutionStore):
    """An execution store whose first ``failures`` writes lose the CAS race."""

    def __init__(
        self, documents: Iterable[ExecutionDocument], *, failures: int
    ) -> None:
        super().__init__(documents)
        self.failures = failures
        self.writes = 0

    def put_phase_result(
        self,
        execution_id: str,
        phase: str,
        artifact: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        self.writes += 1
        if self.writes <= self.failures:
            raise ExecutionStoreConflict("conflict")
        return super().put_phase_result(
            execution_id, phase, artifact, expected_revision=expected_revision
        )


def _ledger(store: InMemoryExecutionStore, execution_id: str) -> AttemptLedger:
    raw = store.get(execution_id).phase_results["experiment_ledger"]["attempts"]
    ledger = AttemptLedger.load(cast(list[Mapping[str, object]], raw))
    ledger.verify()
    return ledger


def _attempts(store: InMemoryExecutionStore, execution_id: str) -> list[AttemptRecord]:
    """Return the attempt records the execution's ledger holds, in order."""
    return [
        entry.record
        for entry in _ledger(store, execution_id).entries()
        if isinstance(entry.record, AttemptRecord)
    ]


def _submit(kit: _Kit, request: Any, phase: str, job_id: str = "job-one") -> None:
    record_submission_exposure(
        kit.workflow,
        request,
        phase=phase,
        job_id=job_id,
        tool_name="run_backtest",
        window_start="2024-01-01T00:00:00Z",
        window_end="2024-01-02T00:00:00Z",
        parameter_sha256="a" * 64,
        phase_attempt_id="attempt-1",
    )


@pytest.mark.parametrize(("phase", "kind"), list(SUBMISSION_KINDS.items()))
def test_submission_records_the_job_with_its_phase_kind(
    tmp_path: Path, phase: str, kind: AttemptKind
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    _submit(kit, request, phase)
    entries = _attempts(kit.execution_store, request.execution_id)
    assert [entry.kind for entry in entries] == [kind]
    payload = entries[0].payload
    assert payload["jobId"] == "job-one"
    assert payload["phase"] == phase
    assert payload["phaseAttemptId"] == "attempt-1"
    assert payload["windowStart"] == "2024-01-01T00:00:00Z"
    assert payload["parameterAssignmentSha256"] == "a" * 64


def test_submission_recording_is_idempotent(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    _submit(kit, request, "null_testing")
    revision = kit.execution_store.get(request.execution_id).revision
    entries = _attempts(kit.execution_store, request.execution_id)
    _submit(kit, request, "null_testing")
    assert kit.execution_store.get(request.execution_id).revision == revision
    assert _attempts(kit.execution_store, request.execution_id) == entries


def test_submission_without_the_durable_store_fails_closed(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    with pytest.raises(LedgerError):
        record_submission_exposure(
            cast(Any, SimpleNamespace()),
            request,
            phase="null_testing",
            job_id="job-one",
            tool_name="run_backtest",
            window_start=None,
            window_end=None,
            parameter_sha256=None,
        )


def test_submission_store_conflict_is_retried_then_recorded(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    store = _ConflictingStore(
        [kit.execution_store.get(request.execution_id)], failures=1
    )
    kit.workflow._execution_store = store
    _submit(kit, request, "variation")
    assert store.writes == 2
    assert len(_attempts(store, request.execution_id)) == 1


def test_submission_store_fault_records_nothing(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    store = _ConflictingStore(
        [kit.execution_store.get(request.execution_id)], failures=2
    )
    kit.workflow._execution_store = store
    with pytest.raises(ExecutionStoreConflict):
        _submit(kit, request, "variation")
    assert store.writes == 2
    assert "experiment_ledger" not in store.get(request.execution_id).phase_results


def test_completions_derive_no_job_backed_attempts(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    completions: dict[str, Mapping[str, object]] = {
        "discovery": {"discovery": _discovery_envelope()},
        "optimization": {"optimization": _candidate_evaluation_envelope()},
        "null_testing": {"nullTesting": _null_test_envelope()},
        "variation": {"variation": _variation_envelope()},
        "strategy_backtesting": {
            "backtest": {
                "jobId": "job-one",
                "state": "succeeded",
                "strategy": {
                    "workspaceId": WORKSPACE,
                    "kind": "strategy",
                    "programId": "momentum",
                    "gitCommit": "a" * 40,
                    "version": "b" * 64,
                },
            }
        },
        "forward_validation": {"forwardValidation": _forward_envelope()},
    }
    for phase, completion in completions.items():
        record_completion_attempts(kit.workflow, request, phase, completion)
    assert (
        "experiment_ledger"
        not in kit.execution_store.get(request.execution_id).phase_results
    )


def test_model_creation_still_records_its_hypothesis_from_the_completion(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    record_completion_attempts(
        kit.workflow,
        request,
        "model_creation",
        {"hypothesis": _hypothesis_envelope()},
    )
    entries = _attempts(kit.execution_store, request.execution_id)
    assert [entry.kind for entry in entries] == [AttemptKind.HYPOTHESIS]


__all__ = ["_Kit"]
