from datetime import UTC, datetime, timedelta
from decimal import Decimal

from harness.core.symbols import Symbol
from harness.core.types import TradeEvent
from harness.data.bars import BarAggregator, build_bars

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)


def trade(
    event_id: str, offset_seconds: int, sequence: int, price: str, quantity: str
) -> TradeEvent:
    return TradeEvent(
        event_id,
        STAMP + timedelta(seconds=offset_seconds),
        sequence,
        Symbol("AAPL"),
        Decimal(price),
        Decimal(quantity),
    )


def test_golden_bars_reconcile_prices_volume_and_boundaries_exactly() -> None:
    trades = (
        trade("t3", 61, 3, "101.25", "0.75"),
        trade("t1", 0, 1, "100.10", "2.5"),
        trade("t2", 30, 2, "99.90", "1.25"),
        trade("t4", 120, 4, "102.00", "3"),
    )

    bars = build_bars(trades, "1min")

    assert [(bar.start, bar.end) for bar in bars] == [
        (STAMP, STAMP + timedelta(minutes=1)),
        (STAMP + timedelta(minutes=1), STAMP + timedelta(minutes=2)),
        (STAMP + timedelta(minutes=2), STAMP + timedelta(minutes=3)),
    ]
    assert [
        (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.trade_count)
        for bar in bars
    ] == [
        (
            Decimal("100.10"),
            Decimal("100.10"),
            Decimal("99.90"),
            Decimal("99.90"),
            Decimal("3.75"),
            2,
        ),
        (
            Decimal("101.25"),
            Decimal("101.25"),
            Decimal("101.25"),
            Decimal("101.25"),
            Decimal("0.75"),
            1,
        ),
        (
            Decimal("102.00"),
            Decimal("102.00"),
            Decimal("102.00"),
            Decimal("102.00"),
            Decimal("3"),
            1,
        ),
    ]
    assert sum((bar.volume for bar in bars), Decimal("0")) == sum(
        (trade.quantity for trade in trades), Decimal("0")
    )


def test_streaming_aggregator_keeps_symbols_separate_only_by_instance() -> None:
    aggregator = BarAggregator(timedelta(minutes=5), symbol="AAPL")
    aggregator.add_many((trade("t1", 0, 1, "100", "1"), trade("t2", 1, 2, "101", "2")))

    bar = aggregator.finalize()[0]
    assert bar.timestamp == STAMP
    assert bar.open == Decimal("100")
    assert bar.close == Decimal("101")
    assert bar.volume == Decimal("3")
