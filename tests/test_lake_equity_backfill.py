from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from lake.catalog import CoverageRecord, CoverageStatus, InMemoryCatalogRepository
from lake.equity_backfill import (
    enqueue_equity_backfill,
    plan_equity_backfill,
)
from lake.partition_paths import AssetClass, DataType
from lake.settings import load_settings
from lake.universes import EquityUniverse, load_universes


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


def _universe(*, maximum_rank: int | None = None) -> EquityUniverse:
    universe = load_universes(Path(__file__).parents[1]).equities.soak
    return universe.model_copy(
        update={
            "symbols": ("AAPL", "MSFT", "SPY"),
            "history": {
                DataType.TRADE: universe.history[DataType.TRADE].model_copy(
                    update={"maximum_liquidity_rank": maximum_rank}
                ),
                DataType.QUOTE: universe.history[DataType.QUOTE],
            },
        }
    )


def test_newest_session_precedes_liquidity_rank_and_symbol_tie_breaks() -> None:
    catalog = InMemoryCatalogRepository(
        [
            _record(
                symbol="AAPL",
                session_date=date(2024, 11, 29),
                status=CoverageStatus.ERROR,
            ),
            _record(
                symbol="MSFT",
                session_date=date(2024, 11, 29),
                status=CoverageStatus.ERROR,
            ),
            _record(
                symbol="SPY",
                session_date=date(2024, 11, 29),
                status=CoverageStatus.ERROR,
            ),
            _record(
                symbol="AAPL",
                session_date=date(2024, 12, 2),
                status=CoverageStatus.ERROR,
            ),
            _record(
                symbol="MSFT",
                session_date=date(2024, 12, 2),
                status=CoverageStatus.ERROR,
            ),
            _record(
                symbol="SPY",
                session_date=date(2024, 12, 2),
                status=CoverageStatus.ERROR,
            ),
        ]
    )

    plan = plan_equity_backfill(
        catalog,
        symbols=("SPY", "MSFT", "AAPL"),
        data_types=DataType.TRADE,
        liquidity_ranks={"SPY": 1, "AAPL": 2, "MSFT": 2},
        start=date(2024, 11, 29),
        end=date(2024, 12, 2),
        as_of=datetime(2024, 12, 3, tzinfo=UTC),
    )

    assert [(item.session_date, item.symbol) for item in plan] == [
        (date(2024, 12, 2), "SPY"),
        (date(2024, 12, 2), "AAPL"),
        (date(2024, 12, 2), "MSFT"),
        (date(2024, 11, 29), "SPY"),
        (date(2024, 11, 29), "AAPL"),
        (date(2024, 11, 29), "MSFT"),
    ]


def test_replanning_resumes_from_catalog_and_skips_terminal_entries() -> None:
    session_date = date(2024, 11, 29)
    catalog = InMemoryCatalogRepository(
        [
            _record(
                symbol="AAPL", session_date=session_date, status=CoverageStatus.COMPLETE
            ),
            _record(
                symbol="MSFT", session_date=session_date, status=CoverageStatus.EMPTY
            ),
            _record(
                symbol="SPY", session_date=session_date, status=CoverageStatus.PARTIAL
            ),
        ]
    )

    first = plan_equity_backfill(
        catalog,
        symbols=("AAPL", "MSFT", "SPY"),
        data_types=DataType.TRADE,
        liquidity_ranks={"AAPL": 1, "MSFT": 2, "SPY": 3},
        start=session_date,
        end=session_date,
        as_of=datetime(2024, 12, 2, tzinfo=UTC),
    )
    assert [item.symbol for item in first] == ["SPY"]

    catalog.upsert(
        _record(symbol="SPY", session_date=session_date, status=CoverageStatus.COMPLETE)
    )
    resumed = plan_equity_backfill(
        catalog,
        symbols=("AAPL", "MSFT", "SPY"),
        data_types=DataType.TRADE,
        liquidity_ranks={"AAPL": 1, "MSFT": 2, "SPY": 3},
        start=session_date,
        end=session_date,
        as_of=datetime(2024, 12, 2, tzinfo=UTC),
    )

    assert not resumed
    assert not resumed.has_more


def test_work_bound_reports_more_work_without_changing_candidate_order() -> None:
    plan = plan_equity_backfill(
        InMemoryCatalogRepository(),
        symbols=("AAPL", "MSFT"),
        data_types=DataType.QUOTE,
        liquidity_ranks={"AAPL": 2, "MSFT": 1},
        max_items=2,
        start=date(2024, 11, 27),
        end=date(2024, 12, 2),
        as_of=datetime(2024, 12, 3, tzinfo=UTC),
    )

    assert [(item.session_date, item.symbol) for item in plan] == [
        (date(2024, 12, 2), "MSFT"),
        (date(2024, 12, 2), "AAPL"),
    ]
    assert plan.available_items == 6
    assert plan.has_more


def test_universe_maximum_rank_filters_only_ineligible_data_type_gaps() -> None:
    plan = plan_equity_backfill(
        InMemoryCatalogRepository(),
        universe=_universe(maximum_rank=2),
        data_types=(DataType.TRADE, DataType.QUOTE),
        liquidity_ranks={"AAPL": 1, "MSFT": 2, "SPY": 3},
        start=date(2024, 11, 29),
        end=date(2024, 11, 29),
        as_of=datetime(2024, 12, 2, tzinfo=UTC),
    )

    assert [item.symbol for item in plan] == ["AAPL", "MSFT", "SPY"]
    assert [item.data_types for item in plan] == [
        (DataType.QUOTE, DataType.TRADE),
        (DataType.QUOTE, DataType.TRADE),
        (DataType.QUOTE,),
    ]


def test_sender_is_injected_and_handler_remains_a_queue_worker_boundary() -> None:
    plan = plan_equity_backfill(
        InMemoryCatalogRepository(),
        symbols="AAPL",
        data_types=DataType.TRADE,
        max_items=1,
        start=date(2024, 11, 29),
        end=date(2024, 11, 29),
        as_of=datetime(2024, 12, 2, tzinfo=UTC),
    )
    messages: list[dict[str, object]] = []

    class Sender:
        def send(self, message: Mapping[str, object]) -> None:
            messages.append(dict(message))

    assert enqueue_equity_backfill(plan, Sender()) == 1
    assert messages[0]["symbol"] == "AAPL"
    assert messages[0]["session_date"] == "2024-11-29"


@pytest.mark.parametrize("bad_limit", [0, -1])
def test_work_bound_must_be_positive(bad_limit: int) -> None:
    with pytest.raises(ValueError, match="max_items must be positive"):
        plan_equity_backfill(
            InMemoryCatalogRepository(),
            symbols="AAPL",
            max_items=bad_limit,
            as_of=datetime(2024, 12, 2, 16, tzinfo=UTC),
        )


def test_settings_enable_or_disable_liquidity_rank_tie_breaking() -> None:
    settings = load_settings(Path(__file__).parents[1]).ingest.backfill
    disabled = settings.model_copy(update={"liquidity_rank_tiebreaker": False})

    plan = plan_equity_backfill(
        InMemoryCatalogRepository(),
        symbols=("AAPL", "MSFT"),
        data_types=DataType.TRADE,
        backfill_settings=disabled,
        liquidity_ranks={"AAPL": 2, "MSFT": 1},
        start=date(2024, 11, 29),
        end=date(2024, 11, 29),
        as_of=datetime(2024, 12, 2, tzinfo=UTC),
    )

    assert [item.symbol for item in plan] == ["AAPL", "MSFT"]
