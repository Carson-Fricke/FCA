from datetime import UTC, date, datetime

import pytest

from lake.catalog import CoverageRecord, CoverageStatus, InMemoryCatalogRepository
from lake.coverage import (
    CoverageState,
    calculate_coverage,
    crypto_sessions,
    equity_sessions,
    plan_gaps,
)
from lake.partition_paths import AssetClass, DataType


def _record(
    *,
    symbol: str,
    data_type: DataType = DataType.TRADE,
    session_date: date,
    status: CoverageStatus,
    asset_class: AssetClass = AssetClass.EQUITY,
) -> CoverageRecord:
    return CoverageRecord(
        asset_class=asset_class,
        data_type=data_type,
        symbol=symbol,
        session_date=session_date,
        status=status,
    )


def test_equity_sessions_use_calendar_holidays_and_half_day_bounds() -> None:
    sessions = equity_sessions(
        date(2024, 11, 27),
        date(2024, 12, 2),
        as_of=datetime(2024, 12, 3, tzinfo=UTC),
    )

    assert [session.session_date for session in sessions] == [
        date(2024, 11, 27),
        date(2024, 11, 29),
        date(2024, 12, 2),
    ]
    assert sessions[1].open_time == datetime(2024, 11, 29, 14, 30, tzinfo=UTC)
    assert sessions[1].close_time == datetime(2024, 11, 29, 18, tzinfo=UTC)
    assert not any(session.is_partial for session in sessions)


def test_crypto_sessions_represent_each_utc_day_and_current_partial_day() -> None:
    sessions = crypto_sessions(
        date(2024, 1, 18),
        date(2024, 1, 19),
        as_of=datetime(2024, 1, 19, 12, tzinfo=UTC),
    )

    assert [(session.session_date, session.is_partial) for session in sessions] == [
        (date(2024, 1, 18), False),
        (date(2024, 1, 19), True),
    ]
    assert sessions[1].open_time == datetime(2024, 1, 19, tzinfo=UTC)
    assert sessions[1].close_time == datetime(2024, 1, 20, tzinfo=UTC)


def test_calculate_coverage_distinguishes_missing_empty_partial_complete_and_error() -> (
    None
):
    catalog = InMemoryCatalogRepository(
        [
            _record(
                symbol="AAPL",
                session_date=date(2024, 11, 27),
                status=CoverageStatus.COMPLETE,
            ),
            _record(
                symbol="AAPL",
                session_date=date(2024, 11, 29),
                status=CoverageStatus.EMPTY,
            ),
            _record(
                symbol="AAPL",
                session_date=date(2024, 12, 2),
                status=CoverageStatus.ERROR,
            ),
        ]
    )

    report = calculate_coverage(
        catalog,
        asset_class=AssetClass.EQUITY,
        symbols="AAPL",
        data_types=DataType.TRADE,
        start=date(2024, 11, 27),
        end=date(2024, 12, 2),
        as_of=datetime(2024, 12, 3, tzinfo=UTC),
    )

    assert [entry.state for entry in report] == [
        CoverageState.COMPLETE,
        CoverageState.EMPTY,
        CoverageState.ERROR,
    ]
    assert report.complete[0].is_complete
    assert report.empty[0].is_terminal
    assert report.errors[0].is_actionable
    assert not report.is_satisfied


def test_current_session_is_partial_and_existing_partition_is_replaceable() -> None:
    session_date = date(2024, 11, 29)
    catalog = InMemoryCatalogRepository(
        [
            _record(
                symbol="AAPL",
                session_date=session_date,
                status=CoverageStatus.COMPLETE,
            )
        ]
    )

    report = calculate_coverage(
        catalog,
        asset_class=AssetClass.EQUITY,
        symbols="AAPL",
        data_types=DataType.TRADE,
        start=session_date,
        end=session_date,
        as_of=datetime(2024, 11, 29, 16, tzinfo=UTC),
    )

    entry = report.entries[0]
    assert entry.state is CoverageState.PARTIAL
    assert entry.session.is_partial
    assert entry.replace_existing
    assert entry.session.open_time == datetime(2024, 11, 29, 14, 30, tzinfo=UTC)
    assert entry.session.close_time == datetime(2024, 11, 29, 18, tzinfo=UTC)


def test_crypto_gap_plan_uses_continuous_utc_days() -> None:
    catalog = InMemoryCatalogRepository(
        [
            _record(
                asset_class=AssetClass.CRYPTO,
                symbol="BTC/USD",
                data_type=DataType.QUOTE,
                session_date=date(2024, 1, 18),
                status=CoverageStatus.COMPLETE,
            ),
            _record(
                asset_class=AssetClass.CRYPTO,
                symbol="BTC/USD",
                data_type=DataType.QUOTE,
                session_date=date(2024, 1, 19),
                status=CoverageStatus.EMPTY,
            ),
        ]
    )

    plan = plan_gaps(
        catalog,
        asset_class=AssetClass.CRYPTO,
        symbols="btc/usd",
        data_types=DataType.QUOTE,
        start=date(2024, 1, 18),
        end=date(2024, 1, 20),
        as_of=datetime(2024, 1, 21, tzinfo=UTC),
    )

    assert [entry.session_date for entry in plan] == [date(2024, 1, 20)]
    assert plan.gaps[0].state is CoverageState.MISSING
    assert plan.gaps[0].session.open_time == datetime(2024, 1, 20, tzinfo=UTC)


def test_gap_plan_excludes_terminal_empty_and_complete_and_sorts_inputs() -> None:
    catalog = InMemoryCatalogRepository(
        [
            _record(
                symbol="AAPL",
                session_date=date(2024, 1, 18),
                status=CoverageStatus.EMPTY,
            ),
            _record(
                symbol="MSFT",
                session_date=date(2024, 1, 18),
                status=CoverageStatus.COMPLETE,
            ),
        ]
    )

    plan = plan_gaps(
        catalog,
        asset_class=AssetClass.EQUITY,
        symbols=[" msft ", "aapl"],
        data_types=[DataType.QUOTE, DataType.TRADE],
        start=date(2024, 1, 18),
        end=date(2024, 1, 19),
        as_of=datetime(2024, 1, 20, tzinfo=UTC),
    )

    assert [(entry.data_type, entry.symbol, entry.session_date) for entry in plan] == [
        (DataType.QUOTE, "AAPL", date(2024, 1, 18)),
        (DataType.QUOTE, "AAPL", date(2024, 1, 19)),
        (DataType.QUOTE, "MSFT", date(2024, 1, 18)),
        (DataType.QUOTE, "MSFT", date(2024, 1, 19)),
        (DataType.TRADE, "AAPL", date(2024, 1, 19)),
        (DataType.TRADE, "MSFT", date(2024, 1, 19)),
    ]
    assert plan.missing == plan.gaps
    assert not plan.partial
    assert not plan.errors


def test_coverage_rejects_unsupported_asset_class_and_naive_as_of() -> None:
    catalog = InMemoryCatalogRepository()

    with pytest.raises(
        ValueError, match="coverage planning supports equity and crypto only"
    ):
        calculate_coverage(
            catalog,
            asset_class=AssetClass.OPTION,
            symbols="AAPL",
            data_types=DataType.TRADE,
            start=date(2024, 1, 18),
            end=date(2024, 1, 18),
        )

    with pytest.raises(ValueError, match="as_of must be timezone-aware"):
        equity_sessions(
            date(2024, 1, 18),
            date(2024, 1, 18),
            as_of=datetime(2024, 1, 18, 12),
        )
