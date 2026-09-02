from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from lake.catalog import CoverageRecord, CoverageStatus, InMemoryCatalogRepository
from lake.equity_catchup import (
    CatchUpOrder,
    enqueue_equity_catch_up,
    plan_equity_catch_up,
)
from lake.partition_paths import AssetClass, DataType


def _record(
    *,
    symbol: str,
    session_date: date,
    status: CoverageStatus,
    data_type: DataType = DataType.TRADE,
) -> CoverageRecord:
    return CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=data_type,
        symbol=symbol,
        session_date=session_date,
        status=status,
    )


def _catalog_for(
    records: list[CoverageRecord],
) -> InMemoryCatalogRepository:
    return InMemoryCatalogRepository(records)


def test_default_plan_targets_latest_completed_session_not_open_session() -> None:
    plan = plan_equity_catch_up(
        _catalog_for(
            [
                _record(
                    symbol="AAPL",
                    session_date=date(2024, 11, 29),
                    status=CoverageStatus.PARTIAL,
                ),
                _record(
                    symbol="AAPL",
                    session_date=date(2024, 11, 29),
                    status=CoverageStatus.COMPLETE,
                    data_type=DataType.QUOTE,
                ),
            ]
        ),
        symbols="AAPL",
        data_types=(DataType.TRADE, DataType.QUOTE),
        as_of=datetime(2024, 12, 2, 16, tzinfo=UTC),
    )

    assert plan.target_session is not None
    assert plan.target_session.session_date == date(2024, 11, 29)
    assert [(item.symbol, item.session_date) for item in plan] == [
        ("AAPL", date(2024, 11, 29))
    ]
    assert plan.items[0].data_types == (DataType.TRADE,)
    assert plan.items[0].replace_existing


def test_range_plan_selects_completed_missing_partial_and_error_but_not_empty() -> None:
    plan = plan_equity_catch_up(
        _catalog_for(
            [
                _record(
                    symbol="AAPL",
                    session_date=date(2024, 11, 27),
                    status=CoverageStatus.EMPTY,
                ),
                _record(
                    symbol="AAPL",
                    session_date=date(2024, 11, 29),
                    status=CoverageStatus.PARTIAL,
                ),
                _record(
                    symbol="MSFT",
                    session_date=date(2024, 12, 2),
                    status=CoverageStatus.ERROR,
                ),
                _record(
                    symbol="SPY",
                    session_date=date(2024, 11, 29),
                    status=CoverageStatus.ERROR,
                ),
            ]
        ),
        symbols=("SPY", "MSFT", "AAPL"),
        data_types=DataType.TRADE,
        start=date(2024, 11, 27),
        end=date(2024, 12, 2),
        as_of=datetime(2024, 12, 2, 16, tzinfo=UTC),
    )

    assert [
        (item.symbol, item.session_date, item.gaps[0].state.value) for item in plan
    ] == [
        ("AAPL", date(2024, 11, 29), "partial"),
        ("MSFT", date(2024, 11, 29), "missing"),
        ("SPY", date(2024, 11, 29), "error"),
        ("MSFT", date(2024, 11, 27), "missing"),
        ("SPY", date(2024, 11, 27), "missing"),
    ]
    assert date(2024, 11, 28) not in plan.session_dates
    assert date(2024, 12, 2) not in plan.session_dates
    assert ("AAPL", date(2024, 11, 27)) not in {
        (item.symbol, item.session_date) for item in plan
    }


@pytest.mark.parametrize(
    ("order", "expected_dates"),
    [
        (
            CatchUpOrder.NEWEST_FIRST,
            [date(2024, 12, 2), date(2024, 11, 29), date(2024, 11, 27)],
        ),
        (
            CatchUpOrder.OLDEST_FIRST,
            [date(2024, 11, 27), date(2024, 11, 29), date(2024, 12, 2)],
        ),
    ],
)
def test_order_is_deterministic_and_only_reverses_session_priority(
    order: CatchUpOrder,
    expected_dates: list[date],
) -> None:
    plan = plan_equity_catch_up(
        _catalog_for([]),
        symbols=("MSFT", "AAPL"),
        data_types=DataType.QUOTE,
        start=date(2024, 11, 27),
        end=date(2024, 12, 2),
        as_of=datetime(2024, 12, 3, tzinfo=UTC),
        order=order,
    )

    assert [item.session_date for item in plan] == [
        session_date for session_date in expected_dates for _ in range(2)
    ]
    assert [item.symbol for item in plan] == [
        symbol for _ in expected_dates for symbol in ("AAPL", "MSFT")
    ]


def test_queue_messages_are_deterministic_and_sender_is_injected() -> None:
    plan = plan_equity_catch_up(
        _catalog_for([]),
        symbols="AAPL",
        data_types=(DataType.TRADE, DataType.QUOTE),
        as_of=datetime(2024, 12, 2, 16, tzinfo=UTC),
    )
    messages: list[dict[str, object]] = []

    class Sender:
        def send(self, message: Mapping[str, object]) -> None:
            messages.append(dict(message))

    assert enqueue_equity_catch_up(plan, Sender()) == 1
    assert messages[0] == {
        "asset_class": "equity",
        "data_types": ["quote", "trade"],
        "end_time": "2024-11-29T18:00:00Z",
        "replace_existing": False,
        "session_date": "2024-11-29",
        "start_time": "2024-11-29T14:30:00Z",
        "symbol": "AAPL",
    }
    assert len(messages) == 1


def test_configuration_can_supply_watchlist_and_newest_first_order() -> None:
    from lake.settings import load_settings
    from lake.universes import load_universes

    root = Path(__file__).parents[1]
    settings = load_settings(root)
    universe = load_universes(root).equities.soak

    plan = plan_equity_catch_up(
        _catalog_for([]),
        universe=universe,
        backfill_settings=settings.ingest.backfill,
        start=date(2024, 11, 29),
        end=date(2024, 11, 29),
        as_of=datetime(2024, 12, 2, 16, tzinfo=UTC),
    )

    assert plan.order is CatchUpOrder.NEWEST_FIRST
    assert [item.symbol for item in plan] == ["AAPL", "SPY"]


def test_invalid_order_and_partial_range_fail_fast() -> None:
    with pytest.raises(ValueError, match="order must be"):
        plan_equity_catch_up(
            _catalog_for([]),
            symbols="AAPL",
            order="random",
            as_of=datetime(2024, 12, 2, 16, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="supplied together"):
        plan_equity_catch_up(
            _catalog_for([]),
            symbols="AAPL",
            start=date(2024, 11, 29),
            as_of=datetime(2024, 12, 2, 16, tzinfo=UTC),
        )


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="as_of must be timezone-aware"):
        plan_equity_catch_up(
            _catalog_for([]),
            symbols="AAPL",
            as_of=datetime(2024, 12, 2, 16),
        )


def test_queue_sender_shape_is_structurally_compatible() -> None:
    plan = plan_equity_catch_up(
        _catalog_for([]),
        symbols="AAPL",
        as_of=datetime(2024, 12, 2, 16, tzinfo=UTC),
    )

    class Sender:
        def send(self, message: Mapping[str, object]) -> None:
            assert isinstance(message, Mapping)

    assert enqueue_equity_catch_up(plan, Sender()) == len(plan)
