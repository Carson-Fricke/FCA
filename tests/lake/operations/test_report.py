"""Focused tests for the daily coverage and budget report."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta

import pytest

from lake.catalog import (
    CoverageRecord,
    CoverageStatus,
    InMemoryCatalogRepository,
    InMemoryJobRepository,
    JobRecord,
    JobState,
)
from lake.configuration.settings import OperationsSettings
from lake.contracts import AssetClass, DataType
from lake.coverage import CoverageReport, calculate_coverage, equity_sessions
from lake.operations import (
    AlertChannel,
    DailyCoverageReport,
    HttpAlertChannel,
    RecordingAlertChannel,
    build_daily_report,
)
from lake.queue import DeadLetterEnvelope, InMemoryDeadLetterStore, QueueMessage
from lake.storage.capacity import (
    StorageCapGuard,
    StorageCapPolicy,
    StorageCapState,
    StorageOperation,
)

_START = date(2024, 1, 16)
_END = date(2024, 1, 19)
_AS_OF = datetime(2024, 2, 1, 12, 0, tzinfo=UTC)


def _settings(*, alerts: tuple[int, ...] = (40, 70)) -> OperationsSettings:
    return OperationsSettings(
        monthly_budget_alerts_usd=alerts,
        log_retention_days=30,
        daily_coverage_report=True,
    )


def _states(
    *overrides: tuple[int, CoverageStatus | None],
) -> list[CoverageStatus | None]:
    sessions = equity_sessions(_START, _END)
    states: list[CoverageStatus | None] = [CoverageStatus.COMPLETE] * len(sessions)
    for index, status in overrides:
        states[index] = status
    return states


def _coverage(states: list[CoverageStatus | None]) -> CoverageReport:
    sessions = equity_sessions(_START, _END)
    assert len(sessions) == len(states)
    records = [
        CoverageRecord(
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol="AAPL",
            session_date=session.session_date,
            status=status,
        )
        for session, status in zip(sessions, states, strict=True)
        if status is not None
    ]
    return calculate_coverage(
        InMemoryCatalogRepository(records),
        asset_class=AssetClass.EQUITY,
        symbols="AAPL",
        data_types=DataType.TRADE,
        start=_START,
        end=_END,
        as_of=_AS_OF,
    )


def _guard(
    *,
    current: int,
    capacity: int = 100,
    pause_enqueue: bool = True,
    pause_ingest: bool = True,
) -> StorageCapGuard:
    return StorageCapGuard(
        storage_usage=lambda: current,
        settings=StorageCapPolicy(
            capacity_bytes=capacity,
            pause_enqueue_at_capacity=pause_enqueue,
            pause_ingest_at_capacity=pause_ingest,
        ),
    )


def _envelope(
    message_id: str, *, error: str, failed_at: datetime
) -> DeadLetterEnvelope:
    return DeadLetterEnvelope.from_failure(
        QueueMessage(
            message_id=message_id,
            pop_receipt=f"receipt-{message_id}",
            content="{}",
            dequeue_count=1,
        ),
        source_queue="equity-backfill",
        error=error,
        failed_at=failed_at,
    )


def _mixed_store() -> InMemoryDeadLetterStore:
    return InMemoryDeadLetterStore(
        [
            _envelope(
                "first",
                error="ValueError: bad close",
                failed_at=_AS_OF - timedelta(hours=3),
            ),
            _envelope(
                "second",
                error="ValueError: bad open",
                failed_at=_AS_OF - timedelta(hours=1),
            ),
            _envelope(
                "third",
                error="KeyError: missing field",
                failed_at=_AS_OF - timedelta(hours=2),
            ),
        ]
    )


def test_all_terminal_coverage_reports_zero_gaps() -> None:
    report = build_daily_report(
        coverage=_coverage(_states((1, CoverageStatus.EMPTY))),
        jobs=InMemoryJobRepository(),
        dead_letters=InMemoryDeadLetterStore(),
        cap_guard=_guard(current=0),
        settings=_settings(),
        as_of=_AS_OF,
    )

    assert report.coverage.gaps == 0
    assert report.coverage.satisfied is True
    assert report.coverage.missing == 0
    assert report.coverage.partial == 0
    assert report.coverage.errors == 0
    assert report.coverage.empty == 1


def test_mixed_coverage_counts_holes_partials_and_errors() -> None:
    report = build_daily_report(
        coverage=_coverage(
            _states(
                (1, CoverageStatus.PARTIAL),
                (2, CoverageStatus.ERROR),
                (3, None),
            )
        ),
        jobs=InMemoryJobRepository(),
        dead_letters=InMemoryDeadLetterStore(),
        cap_guard=_guard(current=0),
        settings=_settings(),
        as_of=_AS_OF,
    )

    coverage = report.coverage
    assert (coverage.missing, coverage.partial, coverage.errors) == (1, 1, 1)
    assert coverage.complete == coverage.total - 3
    assert coverage.gaps == 3
    assert coverage.satisfied is False
    assert coverage.as_dict()["gaps"] == 3


def test_failures_are_grouped_by_type_with_latest_instant() -> None:
    report = build_daily_report(
        coverage=_coverage(_states()),
        jobs=InMemoryJobRepository(),
        dead_letters=_mixed_store(),
        cap_guard=_guard(current=0),
        settings=_settings(),
        as_of=_AS_OF,
    )

    failures = report.failures
    assert failures.total == 3
    assert failures.by_type == (("KeyError", 1), ("ValueError", 2))
    assert failures.latest_failed_at == _AS_OF - timedelta(hours=1)
    assert failures.as_dict()["by_type"] == {"KeyError": 1, "ValueError": 2}


def test_queue_age_uses_created_at_for_in_flight_jobs() -> None:
    jobs = InMemoryJobRepository(
        [
            JobRecord(
                job_id="job-queued",
                state=JobState.QUEUED,
                created_at=_AS_OF - timedelta(hours=2),
                updated_at=_AS_OF,
            ),
            JobRecord(
                job_id="job-running",
                state=JobState.RUNNING,
                created_at=_AS_OF - timedelta(minutes=30),
                updated_at=_AS_OF,
            ),
            JobRecord(
                job_id="job-done",
                state=JobState.SUCCEEDED,
                created_at=_AS_OF - timedelta(days=3),
                updated_at=_AS_OF,
            ),
        ]
    )

    report = build_daily_report(
        coverage=_coverage(_states()),
        jobs=jobs,
        dead_letters=InMemoryDeadLetterStore(),
        cap_guard=_guard(current=0),
        settings=_settings(),
        as_of=_AS_OF,
    )

    queue = report.queue
    assert queue.count == 2
    assert queue.oldest_created_at == _AS_OF - timedelta(hours=2)
    assert queue.oldest_age_seconds == 7200.0
    assert queue.average_age_seconds == 4500.0


def test_paused_state_is_populated_for_both_operations() -> None:
    report = build_daily_report(
        coverage=_coverage(_states()),
        jobs=InMemoryJobRepository(),
        dead_letters=InMemoryDeadLetterStore(),
        cap_guard=_guard(current=100, capacity=100),
        settings=_settings(),
        as_of=_AS_OF,
    )

    assert report.budget.status.state is StorageCapState.PAUSED
    assert report.budget.paused is True
    assert report.budget.paused_operations == ("enqueue", "ingest")
    assert report.budget.as_dict()["paused_operations"] == ["enqueue", "ingest"]


@pytest.mark.parametrize(
    ("current", "pause", "expected_state", "alertable"),
    [
        (50, True, StorageCapState.AVAILABLE, False),
        (100, False, StorageCapState.AT_CAPACITY, True),
        (150, False, StorageCapState.OVER_CAPACITY, True),
    ],
)
def test_budget_alertable_matches_capacity_status(
    current: int,
    pause: bool,
    expected_state: StorageCapState,
    alertable: bool,
) -> None:
    guard = _guard(current=current, pause_enqueue=pause, pause_ingest=pause)
    expected = guard.evaluate(StorageOperation.INGEST)

    report = build_daily_report(
        coverage=_coverage(_states()),
        jobs=InMemoryJobRepository(),
        dead_letters=InMemoryDeadLetterStore(),
        cap_guard=guard,
        settings=_settings(),
        as_of=_AS_OF,
    )

    assert expected.state is expected_state
    assert expected.alertable is alertable
    assert report.budget.alertable == expected.alertable
    assert report.budget.as_dict()["alertable"] == expected.as_alert()["alertable"]
    for key, value in expected.as_alert().items():
        assert report.budget.as_dict()[key] == value
    assert report.budget.as_dict()["current_bytes"] == current
    assert report.budget.as_dict()["alert_levels_usd"] == [40, 70]


def test_report_serialization_is_byte_identical_across_runs() -> None:
    def build() -> DailyCoverageReport:
        return build_daily_report(
            coverage=_coverage(_states((1, CoverageStatus.EMPTY))),
            jobs=InMemoryJobRepository(),
            dead_letters=_mixed_store(),
            cap_guard=_guard(current=100, capacity=100),
            settings=_settings(),
            as_of=_AS_OF,
        )

    first = build()
    second = build()
    first_json = json.dumps(first.as_dict(), ensure_ascii=True, sort_keys=True)
    second_json = json.dumps(second.as_dict(), ensure_ascii=True, sort_keys=True)

    assert first_json == second_json
    assert first.subject() == second.subject()
    payload = first.as_dict()
    assert "generated_at" not in payload
    assert payload["as_of"] == _AS_OF.isoformat()
    assert json.loads(first_json) == payload


def test_recording_channel_receives_the_report() -> None:
    channel = RecordingAlertChannel()
    report = build_daily_report(
        coverage=_coverage(_states()),
        jobs=InMemoryJobRepository(),
        dead_letters=InMemoryDeadLetterStore(),
        cap_guard=_guard(current=0),
        settings=_settings(),
        as_of=_AS_OF,
    )

    channel.send(report)

    assert isinstance(channel, AlertChannel)
    assert channel.reports == [report]


def test_http_channel_uses_injected_transport() -> None:
    class FakeTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bytes, Mapping[str, str]]] = []

        def post(self, url: str, *, body: bytes, headers: Mapping[str, str]) -> int:
            self.calls.append((url, body, headers))
            return 204

    transport = FakeTransport()
    channel = HttpAlertChannel(
        url="https://alerts.example.test/daily",
        transport=transport,
        headers={"authorization": "Bearer token"},
    )
    report = build_daily_report(
        coverage=_coverage(_states()),
        jobs=InMemoryJobRepository(),
        dead_letters=InMemoryDeadLetterStore(),
        cap_guard=_guard(current=0),
        settings=_settings(),
        as_of=_AS_OF,
    )

    status = channel.send(report)

    assert status == 204
    assert len(transport.calls) == 1
    url, body, headers = transport.calls[0]
    assert url == "https://alerts.example.test/daily"
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == "Bearer token"
    assert json.loads(body.decode("utf-8")) == report.as_dict()
    assert channel.last_status == 204
