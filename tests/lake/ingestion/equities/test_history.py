from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from lake.catalog import InMemoryCatalogRepository
from lake.configuration import load_universes
from lake.contracts import DataType
from lake.ingestion.equities.backfill import plan_equity_backfill
from lake.ingestion.equities.history import HistoryLimit, HistoryValve

ROOT = Path(__file__).parents[4]


def test_trade_and_quote_limits_are_independent_and_explicit() -> None:
    valve = HistoryValve.from_limits(trade=1, quote=2)

    assert valve.trade == HistoryLimit(DataType.TRADE, 1)
    assert valve.quote == HistoryLimit(DataType.QUOTE, 2)
    assert valve.allows(DataType.TRADE, liquidity_rank=1)
    assert not valve.allows(DataType.TRADE, liquidity_rank=2)
    assert valve.allows(DataType.QUOTE, liquidity_rank=2)
    assert not valve.allows(DataType.QUOTE, liquidity_rank=3)
    assert not valve.allows(DataType.QUOTE, liquidity_rank=None)


def test_universe_history_limits_translate_to_the_valve() -> None:
    universe = load_universes(ROOT).equities.soak
    limited = universe.model_copy(
        update={
            "history": {
                DataType.TRADE: universe.history[DataType.TRADE].model_copy(
                    update={"maximum_liquidity_rank": 1}
                ),
                DataType.QUOTE: universe.history[DataType.QUOTE].model_copy(
                    update={"maximum_liquidity_rank": 2}
                ),
            }
        }
    )
    valve = HistoryValve.from_universe(limited)

    assert valve.trade.maximum_liquidity_rank == 1
    assert valve.quote.maximum_liquidity_rank == 2


def test_backfill_applies_explicit_valve_per_data_type() -> None:
    plan = plan_equity_backfill(
        InMemoryCatalogRepository(),
        symbols=("AAPL", "MSFT"),
        data_types=(DataType.TRADE, DataType.QUOTE),
        liquidity_ranks={"AAPL": 1, "MSFT": 2},
        history_valve=HistoryValve.from_limits(trade=1, quote=2),
        start=date(2024, 1, 19),
        end=date(2024, 1, 19),
        as_of=datetime(2024, 1, 22, tzinfo=UTC),
    )

    assert [item.symbol for item in plan] == ["AAPL", "MSFT"]
    assert [item.data_types for item in plan] == [
        (DataType.QUOTE, DataType.TRADE),
        (DataType.QUOTE,),
    ]


@pytest.mark.parametrize("bad_rank", [0, -1, True])
def test_history_limits_reject_non_positive_ranks(bad_rank: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        HistoryLimit(DataType.QUOTE, bad_rank)
