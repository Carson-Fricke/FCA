"""Parallel sweep orchestration over an injected job-launcher port.

The runner is a deterministic orchestrator: it expands a
:class:`~harness.sweeps.spec.SweepSpec` into deduplicated points, persists one
sweep record mapping the sweep ID to its run IDs, and submits every derived
configuration through an injected launcher port. Parallelism is the
launcher's concern — production compositions submit to Azure Container Apps
Jobs (the T112 resource), while :class:`harness.http.launcher.InProcessBacktestLauncher`
satisfies the same port in-process.

Sweep records persist as canonical JSON below ``sweeps/{sweep_id}/sweep.json``
through the same readable-blob port the results store uses, so the record and
the run catalog share one storage adapter. Live status is never stored: the
runner joins the persisted run IDs against
:class:`~harness.results.store.BacktestResultsStore` catalog rows, so
completion state comes from wherever the launcher actually recorded the runs.

Launch submission contract (what a Container Apps Job client must honor):
the port is called once per deduplicated run with a
:class:`harness.http.launcher.BacktestLaunchJob` whose ``job_id`` is the
derived ``run_id`` and whose ``configuration`` is the full validated
``BacktestConfig``. The call must enqueue the job and return ``None``
synchronously without waiting for completion; the client owns queueing,
retry, and Container Apps parallelism, and completed runs must land in the
shared results store catalog under the same ``run_id`` so sweep status and
sensitivity can observe them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from harness.configuration.hashing import canonical_configuration_bytes
from harness.http.launcher import BacktestLaunchJob
from harness.results.store import BacktestResultsStore, RunBlobStore, RunStatus
from harness.sweeps.spec import SweepPoint, SweepSpec

_SWEEP_PREFIX: Final = "sweeps"
_RECORD_NAME: Final = "sweep.json"
_RECORD_VERSION: Final = "harness.sweeps.record.v1"
_HEXA_DIGEST: Final = 64


class SweepJobLauncher(Protocol):
    """Submission boundary for one derived sweep run.

    Synchronous enqueue semantics: implementations schedule the run and
    return without waiting for completion, mirroring an HTTP 202 acceptance.
    :class:`harness.http.launcher.InProcessBacktestLauncher` satisfies this
    port by running synchronously to completion.
    """

    def __call__(self, job: BacktestLaunchJob) -> None: ...


def sweep_prefix(sweep_id: str) -> str:
    """Return the blob prefix holding one sweep's record."""
    if (
        not isinstance(sweep_id, str)
        or len(sweep_id) != _HEXA_DIGEST
        or any(character not in "0123456789abcdef" for character in sweep_id)
    ):
        raise ValueError("sweep_id must be a 64-character lowercase sha-256 hex digest")
    return f"{_SWEEP_PREFIX}/{sweep_id}"


@dataclass(frozen=True, slots=True)
class SweepSubmission:
    """Outcome of submitting one sweep for execution."""

    sweep_id: str
    requested: int
    expanded: int
    duplicates_skipped: int
    run_ids: tuple[str, ...]
    points: tuple[SweepPoint, ...]


@dataclass(frozen=True, slots=True)
class SweepRunView:
    """One sweep run joined against the results catalog."""

    run_id: str
    status: RunStatus | None
    metrics: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SweepStatus:
    """Queryable sweep state derived from the run catalog."""

    sweep_id: str
    requested: int
    expanded: int
    runs: tuple[SweepRunView, ...]

    @property
    def complete(self) -> int:
        """Count runs the catalog recorded as complete."""
        return sum(view.status is RunStatus.COMPLETE for view in self.runs)

    @property
    def failed(self) -> int:
        """Count runs the catalog recorded as failed."""
        return sum(view.status is RunStatus.FAILED for view in self.runs)

    @property
    def pending(self) -> int:
        """Count runs not yet cataloged or still queued or running."""
        return sum(
            view.status is None or view.status in {RunStatus.QUEUED, RunStatus.RUNNING}
            for view in self.runs
        )

    @property
    def is_complete(self) -> bool:
        """Return whether every deduplicated run is complete and none failed."""
        return bool(self.runs) and self.pending == 0 and self.failed == 0


def _record_payload(
    spec: SweepSpec, points: tuple[SweepPoint, ...]
) -> dict[str, object]:
    return {
        "version": _RECORD_VERSION,
        "sweep_id": spec.sweep_id,
        "mode": spec.mode.value,
        "requested": spec.count,
        "runs": [
            {
                "index": point.index,
                "run_id": point.run_id,
                "configuration_digest": point.configuration_digest,
                "parameters": dict(point.parameters),
            }
            for point in points
        ],
    }


class SweepRunner:
    """Expand, deduplicate, submit, and track one parameter sweep."""

    def __init__(
        self,
        launcher: SweepJobLauncher,
        store: BacktestResultsStore,
        blobs: RunBlobStore,
    ) -> None:
        """Wire the launcher port and the shared storage adapters.

        ``store`` is the results store the launcher records runs into and
        ``blobs`` must be the same blob port backing that store, so sweep
        records and run artifacts share one storage boundary.
        """
        self._launcher = launcher
        self._store = store
        self._blobs = blobs

    def submit(self, spec: SweepSpec) -> SweepSubmission:
        """Deduplicate the spec, persist its record, and launch every point.

        The sweep record is written before any launch, so an exception from
        one submission still leaves the sweep queryable with the remaining
        runs pending. Re-submitting an identical spec is idempotent: the same
        sweep ID, run IDs, and record bytes are reproduced, and execution
        retry policy belongs to the launcher.
        """
        points = spec.expand()
        record_path = f"{sweep_prefix(spec.sweep_id)}/{_RECORD_NAME}"
        self._blobs.replace(
            record_path, canonical_configuration_bytes(_record_payload(spec, points))
        )
        for point in points:
            self._launcher(
                BacktestLaunchJob(
                    job_id=point.run_id, configuration=point.configuration
                )
            )
        return SweepSubmission(
            sweep_id=spec.sweep_id,
            requested=spec.count,
            expanded=len(points),
            duplicates_skipped=spec.count - len(points),
            run_ids=tuple(point.run_id for point in points),
            points=points,
        )

    def _run_view(self, run_id: str) -> SweepRunView:
        record = self._store.get(run_id)
        if record is None:
            return SweepRunView(run_id=run_id, status=None, metrics={})
        return SweepRunView(
            run_id=record.run_id,
            status=record.status,
            metrics=record.metrics,
        )

    def status(self, sweep_id: str) -> SweepStatus:
        """Return one sweep's state joined against the run catalog."""
        prefix = sweep_prefix(sweep_id)
        try:
            raw = self._blobs.read(f"{prefix}/{_RECORD_NAME}")
        except KeyError:
            raise KeyError(f"sweep not found: {sweep_id}") from None
        document = json.loads(raw)
        runs = tuple(self._run_view(entry["run_id"]) for entry in document["runs"])
        return SweepStatus(
            sweep_id=sweep_id,
            requested=document["requested"],
            expanded=len(runs),
            runs=runs,
        )

    def sweep_ids(self) -> tuple[str, ...]:
        """Return every persisted sweep ID in sorted order."""
        prefix = f"{_SWEEP_PREFIX}/"
        suffix = f"/{_RECORD_NAME}"
        return tuple(
            path[len(prefix) : -len(suffix)]
            for path in self._blobs.list_prefix(prefix)
            if path.endswith(suffix)
        )


__all__ = [
    "SweepJobLauncher",
    "SweepRunView",
    "SweepRunner",
    "SweepStatus",
    "SweepSubmission",
    "sweep_prefix",
]
