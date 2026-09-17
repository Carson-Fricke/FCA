"""Provider-independent coverage and ensure use cases."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import date, datetime
from threading import Event, Thread
from typing import Any, Protocol, cast

from lake.catalog.models import JobRecord, JobState
from lake.catalog.ports import CatalogRepository, JobRepository
from lake.contracts import AssetClass, DataType
from lake.coverage.service import (
    CoverageReport,
    SessionCoverage,
    calculate_coverage,
)


class CoverageRequestError(ValueError):
    """A coverage request rejected by an application policy."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: object | None = None,
    ) -> None:
        self.code = code
        self.details = details
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EnsureSpec:
    """Provider-independent coverage request."""

    asset_class: AssetClass
    data_type: DataType
    symbol: str
    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True)
class EnsureJob:
    """Work description passed to an injected ensure launcher."""

    job_id: str
    request: EnsureSpec
    gaps: tuple[SessionCoverage, ...]


EnsureJobLauncher = Callable[[EnsureJob], None | Awaitable[None]]

_IN_FLIGHT_JOB_STATES = frozenset({JobState.QUEUED, JobState.RUNNING})


class EnsureRequestLike(Protocol):
    @property
    def asset_class(self) -> AssetClass: ...

    @property
    def data_type(self) -> DataType: ...

    @property
    def symbol(self) -> str: ...

    @property
    def start_date(self) -> date: ...

    @property
    def end_date(self) -> date: ...


def requested_coverage(
    catalog: CatalogRepository,
    spec: EnsureSpec,
    *,
    as_of: datetime,
    operation: str,
) -> CoverageReport:
    """Validate and calculate one coverage request.

    The request covers any window, including full history: ensure exists to
    make a period complete, and no date-range cap bounds it.
    """
    if spec.asset_class not in {AssetClass.EQUITY, AssetClass.CRYPTO}:
        subject = "coverage" if operation == "catalog" else operation
        raise CoverageRequestError(
            "unsupported_asset_class",
            f"{subject} planning supports equity and crypto only"
            if operation != "query"
            else "file queries support equity and crypto only",
        )
    return calculate_coverage(
        catalog,
        asset_class=spec.asset_class,
        symbols=spec.symbol,
        data_types=spec.data_type,
        start=spec.start_date,
        end=spec.end_date,
        as_of=as_of,
    )


async def ensure_coverage(
    catalog: CatalogRepository,
    jobs: JobRepository,
    launcher: EnsureJobLauncher,
    spec: EnsureSpec,
    *,
    as_of: datetime,
    wait_deadline_seconds: float,
) -> CoverageReport | JobRecord:
    """Return satisfied coverage or launch one stable asynchronous job.

    A persisted job record short-circuits repeated calls while it is still in
    flight. A terminal record whose coverage remains unsatisfied is superseded
    and the work is relaunched, so re-ensuring the same spec repairs sessions
    that ended in error or partial states. The record is marked running while
    the launcher works and reaches a terminal state (``complete`` or ``failed``)
    as soon as it returns, even when the HTTP caller's wait deadline expires.
    Ensure requests are unbounded: the spec may span any window, including
    full history.
    """
    report = requested_coverage(
        catalog,
        spec,
        as_of=as_of,
        operation="ensure",
    )
    if report.is_satisfied:
        return report

    job_id = ensure_job_id(spec)
    existing = jobs.get(job_id)
    if existing is not None and existing.state in _IN_FLIGHT_JOB_STATES:
        return existing

    created_at = existing.created_at if existing is not None else as_of
    record = jobs.upsert(
        JobRecord(
            job_id=job_id,
            state=JobState.RUNNING,
            created_at=created_at,
            updated_at=as_of,
        )
    )

    def finalize(error: Exception | None) -> None:
        jobs.upsert(
            JobRecord(
                job_id=job_id,
                state=JobState.FAILED if error is not None else JobState.COMPLETE,
                created_at=created_at,
                updated_at=as_of,
                error_message=None if error is None else str(error),
            )
        )

    job = EnsureJob(job_id=job_id, request=spec, gaps=report.gap_plan().gaps)
    completed = False
    try:
        completed = await _invoke_launcher(
            launcher,
            job,
            timeout=wait_deadline_seconds,
            on_complete=finalize,
        )
    except Exception as error:
        record = jobs.upsert(
            JobRecord(
                job_id=job_id,
                state=JobState.FAILED,
                created_at=created_at,
                updated_at=as_of,
                error_message=str(error),
            )
        )
    if completed:
        refreshed = requested_coverage(
            catalog,
            spec,
            as_of=as_of,
            operation="ensure",
        )
        if refreshed.is_satisfied:
            return refreshed
    return jobs.get(job_id) or record


def ensure_job_id(request: EnsureRequestLike) -> str:
    """Return the stable identifier for a logically equivalent ensure request."""
    payload = {
        "asset_class": str(request.asset_class),
        "data_type": str(request.data_type),
        "symbol": str(request.symbol).strip().upper(),
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"ensure-{hashlib.sha256(encoded).hexdigest()}"


async def _invoke_launcher(
    launcher: EnsureJobLauncher,
    job: EnsureJob,
    *,
    timeout: float,
    on_complete: Callable[[Exception | None], None] | None = None,
) -> bool:
    """Run one ensure launcher and report whether it finished before the wait.

    ``on_complete`` runs on the worker thread as soon as the launcher returns -
    including after the HTTP caller's wait deadline has expired - so the job
    record always reaches a terminal state.
    """
    completed = Event()
    errors: list[Exception] = []

    def run() -> None:
        try:
            result = launcher(job)
            if inspect.isawaitable(result):
                asyncio.run(cast(Coroutine[Any, Any, None], result))
        except Exception as error:
            errors.append(error)
        finally:
            try:
                if on_complete is not None:
                    on_complete(errors[0] if errors else None)
            finally:
                completed.set()

    Thread(target=run, daemon=True).start()
    if not await asyncio.to_thread(completed.wait, timeout):
        return False
    if errors:
        raise errors[0]
    return True


__all__ = [
    "CoverageRequestError",
    "EnsureJob",
    "EnsureJobLauncher",
    "EnsureSpec",
    "ensure_coverage",
    "ensure_job_id",
    "requested_coverage",
]
