"""Daily operations reporting for coverage, failures, and budget pressure.

Every input to a report is injected: the coverage report, job repository,
dead-letter store, capacity guard, typed operations settings, the as-of
instant, and the alert channel transport.  Nothing in this module reads the
wall clock or opens a network socket, so a report built from the same inputs
serializes byte-for-byte identically across runs.

The report deliberately carries no ``generated_at`` field: ``as_of`` is
supplied by the caller and is the only time reference in the payload.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol, runtime_checkable

from lake.catalog.models import JobState
from lake.catalog.ports import JobRepository
from lake.configuration.settings import OperationsSettings
from lake.contracts import AssetClass
from lake.coverage.service import CoverageReport
from lake.queue.dead_letter import DeadLetterStore
from lake.storage.capacity import (
    StorageCapGuard,
    StorageCapStatus,
    StorageOperation,
)

_IN_FLIGHT_JOB_STATES = frozenset({JobState.QUEUED, JobState.RUNNING})


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Session counts for one asset class and inclusive date range."""

    asset_class: AssetClass
    start: date
    end: date
    missing: int
    partial: int
    errors: int
    delisted: int
    empty: int
    complete: int
    gaps: int
    total: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        if self.total < 0 or self.gaps < 0:
            raise ValueError("coverage totals must not be negative")

    @property
    def satisfied(self) -> bool:
        """Whether every expected session is terminally complete or empty."""
        return self.gaps == 0

    def as_dict(self) -> dict[str, object]:
        """Return stable, JSON-ready fields for the coverage section."""
        return {
            "asset_class": self.asset_class.value,
            "complete": self.complete,
            "delisted": self.delisted,
            "empty": self.empty,
            "end": self.end.isoformat(),
            "errors": self.errors,
            "gaps": self.gaps,
            "missing": self.missing,
            "partial": self.partial,
            "satisfied": self.satisfied,
            "start": self.start.isoformat(),
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class FailureSummary:
    """Dead-letter counts grouped by exception type."""

    total: int
    by_type: tuple[tuple[str, int], ...] = ()
    latest_failed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("failure total must not be negative")
        if sum(count for _, count in self.by_type) != self.total:
            raise ValueError("failure by_type counts must sum to total")
        if self.latest_failed_at is not None:
            object.__setattr__(self, "latest_failed_at", _as_utc(self.latest_failed_at))

    def as_dict(self) -> dict[str, object]:
        """Return stable, JSON-ready fields for the failure section."""
        return {
            "by_type": {error_type: count for error_type, count in self.by_type},
            "latest_failed_at": (
                None
                if self.latest_failed_at is None
                else self.latest_failed_at.isoformat()
            ),
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class QueueAgeSummary:
    """Age of queued and running jobs measured from their creation instant.

    ``QueueMessage`` carries no enqueue timestamp, and ``JobRecord`` has no
    start instant, so ``created_at`` is the best available proxy.  A running
    job's age is therefore a lower bound on its actual run time.
    """

    count: int
    oldest_created_at: datetime | None = None
    oldest_age_seconds: float | None = None
    average_age_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("queue count must not be negative")
        if self.oldest_created_at is not None:
            object.__setattr__(
                self, "oldest_created_at", _as_utc(self.oldest_created_at)
            )

    def as_dict(self) -> dict[str, object]:
        """Return stable, JSON-ready fields for the queue-age section."""
        return {
            "average_age_seconds": self.average_age_seconds,
            "count": self.count,
            "oldest_age_seconds": self.oldest_age_seconds,
            "oldest_created_at": (
                None
                if self.oldest_created_at is None
                else self.oldest_created_at.isoformat()
            ),
        }


@dataclass(frozen=True, slots=True)
class BudgetSummary:
    """Stored bytes, projected cap use, and paused operations.

    The byte values come from the injected capacity statuses, never from a
    live reader.  ``status`` is the projected ingest evaluation; ``enqueue``
    captures the independent enqueue pause switch.  ``alert_levels_usd`` are
    the configured USD thresholds carried for the operator, while the
    ``alertable`` flag is derived from the byte cap status.
    """

    status: StorageCapStatus
    enqueue: StorageCapStatus
    alert_levels_usd: tuple[int, ...] = ()

    @property
    def alertable(self) -> bool:
        """Whether projected ingest use would reach or cross the cap."""
        return self.status.alertable

    @property
    def paused(self) -> bool:
        """Whether any capacity operation is currently paused."""
        return self.status.paused or self.enqueue.paused

    @property
    def paused_operations(self) -> tuple[str, ...]:
        """Return paused operation names in stable enqueue/ingest order."""
        return tuple(
            candidate.operation.value
            for candidate in (self.enqueue, self.status)
            if candidate.paused
        )

    def as_dict(self) -> dict[str, object]:
        """Return the ingest alert fields plus the enqueue and pause state."""
        payload: dict[str, object] = dict(self.status.as_alert())
        payload["alert_levels_usd"] = list(self.alert_levels_usd)
        payload["enqueue"] = self.enqueue.as_alert()
        payload["paused_operations"] = list(self.paused_operations)
        return payload


@dataclass(frozen=True, slots=True)
class DailyCoverageReport:
    """One deterministic daily view of coverage, failures, and budget."""

    as_of: datetime
    coverage: CoverageSummary
    failures: FailureSummary
    queue: QueueAgeSummary
    budget: BudgetSummary

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _as_utc(self.as_of))

    def as_dict(self) -> dict[str, object]:
        """Return the stable, sortable JSON-ready report payload."""
        return {
            "as_of": self.as_of.isoformat(),
            "budget": self.budget.as_dict(),
            "coverage": self.coverage.as_dict(),
            "failures": self.failures.as_dict(),
            "queue": self.queue.as_dict(),
        }

    def subject(self) -> str:
        """Return a concise, deterministic alert subject line."""
        alert = " alert" if self.budget.alertable else ""
        return (
            f"lake daily report as_of={self.as_of.isoformat()} "
            f"gaps={self.coverage.gaps} failures={self.failures.total} "
            f"queued={self.queue.count} capacity={self.budget.status.state.value}"
            f"{alert}"
        )


def build_daily_report(
    *,
    coverage: CoverageReport,
    jobs: JobRepository,
    dead_letters: DeadLetterStore,
    cap_guard: StorageCapGuard,
    settings: OperationsSettings,
    as_of: datetime,
) -> DailyCoverageReport:
    """Build one deterministic report from injected inputs.

    The capacity guard is evaluated for both ingest and enqueue so paused
    states are explicit.  ``calculate_coverage`` defaults to the wall clock,
    so callers must pass an explicit ``as_of`` to that function.
    """
    normalized_as_of = _as_utc(as_of)
    ingest_status = cap_guard.evaluate(StorageOperation.INGEST)
    enqueue_status = cap_guard.evaluate(StorageOperation.ENQUEUE)
    return DailyCoverageReport(
        as_of=normalized_as_of,
        coverage=_coverage_summary(coverage),
        failures=_failure_summary(dead_letters),
        queue=_queue_age_summary(jobs, normalized_as_of),
        budget=BudgetSummary(
            status=ingest_status,
            enqueue=enqueue_status,
            alert_levels_usd=tuple(settings.monthly_budget_alerts_usd),
        ),
    )


def _coverage_summary(report: CoverageReport) -> CoverageSummary:
    return CoverageSummary(
        asset_class=report.asset_class,
        start=report.start,
        end=report.end,
        missing=len(report.missing),
        partial=len(report.partial),
        errors=len(report.errors),
        delisted=len(report.delisted),
        empty=len(report.empty),
        complete=len(report.complete),
        gaps=len(report.gap_plan().gaps),
        total=len(report),
    )


def _failure_summary(store: DeadLetterStore) -> FailureSummary:
    counts: dict[str, int] = {}
    latest: datetime | None = None
    envelopes = store.list()
    for envelope in envelopes:
        counts[envelope.error_type] = counts.get(envelope.error_type, 0) + 1
        if latest is None or envelope.failed_at > latest:
            latest = envelope.failed_at
    return FailureSummary(
        total=len(envelopes),
        by_type=tuple(sorted(counts.items())),
        latest_failed_at=latest,
    )


def _queue_age_summary(jobs: JobRepository, as_of: datetime) -> QueueAgeSummary:
    in_flight = tuple(job for job in jobs.list() if job.state in _IN_FLIGHT_JOB_STATES)
    if not in_flight:
        return QueueAgeSummary(count=0)
    ages = [max(0.0, (as_of - job.created_at).total_seconds()) for job in in_flight]
    oldest = min(job.created_at for job in in_flight)
    return QueueAgeSummary(
        count=len(in_flight),
        oldest_created_at=oldest,
        oldest_age_seconds=round(max(ages), 3),
        average_age_seconds=round(sum(ages) / len(ages), 3),
    )


@runtime_checkable
class AlertChannel(Protocol):
    """Inexpensive delivery boundary for one daily report."""

    def send(self, report: DailyCoverageReport) -> object:
        """Deliver one report and return any transport-specific receipt."""


class AlertTransport(Protocol):
    """Injected HTTP transport so alert delivery never opens a socket here."""

    def post(self, url: str, *, body: bytes, headers: Mapping[str, str]) -> int:
        """Perform one HTTP POST and return the response status code."""


class HttpAlertChannel:
    """Post one JSON report to an alert webhook over an injected transport."""

    def __init__(
        self,
        *,
        url: str,
        transport: AlertTransport,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if not url.strip():
            raise ValueError("alert url must not be empty")
        self._url = url
        self._transport = transport
        self._headers = dict(headers) if headers is not None else {}
        self.last_status: int | None = None

    def send(self, report: DailyCoverageReport) -> int:
        """Serialize and post the report, recording the response status."""
        body = json.dumps(
            report.as_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        headers = {"content-type": "application/json", **self._headers}
        status = self._transport.post(self._url, body=body, headers=headers)
        self.last_status = status
        return status


class RecordingAlertChannel:
    """Deterministic offline channel that retains delivered reports."""

    def __init__(self) -> None:
        self.reports: list[DailyCoverageReport] = []

    def send(self, report: DailyCoverageReport) -> None:
        """Retain one report without performing any IO."""
        self.reports.append(report)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "AlertChannel",
    "AlertTransport",
    "BudgetSummary",
    "CoverageSummary",
    "DailyCoverageReport",
    "FailureSummary",
    "HttpAlertChannel",
    "QueueAgeSummary",
    "RecordingAlertChannel",
    "build_daily_report",
]
