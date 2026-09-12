"""Focused tests for the daily report composition entrypoint."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from lake.catalog import (
    CoverageRecord,
    CoverageStatus,
    InMemoryCatalogRepository,
    InMemoryJobRepository,
)
from lake.configuration.settings import OperationsSettings
from lake.contracts import AssetClass, DataType
from lake.coverage import CoverageReport, calculate_coverage, equity_sessions
from lake.operations import (
    AlertChannel,
    DailyCoverageReport,
    RecordingAlertChannel,
    main,
)
from lake.queue import InMemoryDeadLetterStore
from lake.storage.capacity import StorageCapGuard, StorageCapPolicy

_START = date(2024, 1, 16)
_END = date(2024, 1, 19)
_AS_OF = datetime(2024, 2, 1, 12, 0, tzinfo=UTC)


def _coverage() -> CoverageReport:
    records = [
        CoverageRecord(
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol="AAPL",
            session_date=session.session_date,
            status=CoverageStatus.COMPLETE,
        )
        for session in equity_sessions(_START, _END)
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


def _settings() -> OperationsSettings:
    return OperationsSettings(
        monthly_budget_alerts_usd=(40, 70),
        log_retention_days=30,
        daily_coverage_report=True,
    )


def _guard() -> StorageCapGuard:
    return StorageCapGuard(
        storage_usage=lambda: 0,
        settings=StorageCapPolicy(capacity_bytes=100),
    )


def _run(argv: Sequence[str], channel: AlertChannel) -> int:
    return main(
        argv,
        coverage=_coverage(),
        jobs=InMemoryJobRepository(),
        dead_letters=InMemoryDeadLetterStore(),
        cap_guard=_guard(),
        settings=_settings(),
        channel=channel,
        as_of=_AS_OF,
    )


def test_main_emits_json_and_returns_zero(capsys: Any) -> None:
    channel = RecordingAlertChannel()

    code = _run([], channel)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["as_of"] == _AS_OF.isoformat()
    assert set(payload) == {"as_of", "budget", "coverage", "failures", "queue"}
    assert len(channel.reports) == 1
    assert channel.reports[0].as_dict() == payload


def test_main_dry_run_skips_the_channel(capsys: Any) -> None:
    channel = RecordingAlertChannel()

    code = _run(["--dry-run"], channel)

    assert code == 0
    assert channel.reports == []
    assert json.loads(capsys.readouterr().out)["as_of"] == _AS_OF.isoformat()


def test_main_returns_one_when_channel_send_fails(capsys: Any) -> None:
    class FailingChannel:
        def send(self, report: DailyCoverageReport) -> None:
            raise RuntimeError("webhook unavailable")

    code = _run([], FailingChannel())

    assert code == 1
    assert "webhook unavailable" in capsys.readouterr().err
