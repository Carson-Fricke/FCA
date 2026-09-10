"""Golden-value tests for the tick-native indicator library.

Every expected sequence is hand-computed from the definition of the
indicator over the stated tick stream, using only exact decimal arithmetic
(spans and periods are chosen so smoothing factors terminate in decimal).
Sample values are compared by exact numeric equality and sample stamps by
the closed bucket's exclusive end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from harness.core.symbols import Symbol
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.indicators import (
    AverageTrueRange,
    DollarVolume,
    ExponentialMovingAverage,
    MidSimpleMovingAverage,
    OnBalanceVolume,
    RealizedVolatility,
    RelativeStrengthIndex,
    SimpleMovingAverage,
    VolumeWeightedAveragePrice,
    bucket_start,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def trade(
    offset_seconds: int, sequence: int, price: str, quantity: str, symbol: str = "AAPL"
) -> TradeEvent:
    return TradeEvent(
        f"t{sequence}",
        STAMP + timedelta(seconds=offset_seconds),
        sequence,
        Symbol(symbol),
        Decimal(price),
        Decimal(quantity),
    )


def quote(
    offset_seconds: int, sequence: int, bid: str, ask: str, symbol: str = "AAPL"
) -> QuoteEvent:
    return QuoteEvent(
        f"q{sequence}",
        STAMP + timedelta(seconds=offset_seconds),
        sequence,
        Symbol(symbol),
        Decimal(bid),
        Decimal("1"),
        Decimal(ask),
        Decimal("1"),
    )


def run(indicator, events) -> list[tuple[datetime, Decimal]]:
    for event in events:
        indicator.on_event(event)
    indicator.flush()
    return [(sample.end, sample.value) for sample in indicator.samples()]


def test_sma_golden_two_period() -> None:
    indicator = SimpleMovingAverage("AAPL", period=2)
    result = run(
        indicator,
        (
            trade(0, 1, "100.10", "2.5"),
            trade(30, 2, "99.90", "1.25"),
            trade(61, 3, "101.25", "0.75"),
            trade(120, 4, "102.00", "3"),
        ),
    )
    assert result == [
        (STAMP + MINUTE, Decimal("100.00")),
        (STAMP + 2 * MINUTE, Decimal("100.575")),
        (STAMP + 3 * MINUTE, Decimal("101.625")),
    ]


def test_sma_golden_four_period_matches_independent_sum() -> None:
    prices = ("100.10", "99.90", "101.25", "102.00")
    indicator = SimpleMovingAverage("AAPL", period=4)
    result = run(
        indicator,
        (
            trade(0, 1, prices[0], "1"),
            trade(30, 2, prices[1], "1"),
            trade(61, 3, prices[2], "1"),
            trade(120, 4, prices[3], "1"),
        ),
    )
    assert result == [(STAMP + 3 * MINUTE, Decimal("100.8125"))]
    assert result[0][1] == sum((Decimal(price) for price in prices), Decimal("0")) / 4


def test_sma_warmup_needs_full_window() -> None:
    indicator = SimpleMovingAverage("AAPL", period=2)
    assert run(indicator, (trade(0, 1, "100.10", "1"),)) == []


def test_ema_golden_span_three_with_exact_alpha() -> None:
    indicator = ExponentialMovingAverage("AAPL", span=3)
    result = run(
        indicator,
        (
            trade(0, 1, "100.10", "1"),
            trade(30, 2, "99.90", "1"),
            trade(61, 3, "101.25", "1"),
            trade(120, 4, "102.00", "1"),
        ),
    )
    assert result == [
        (STAMP + 2 * MINUTE, Decimal("100.625")),
        (STAMP + 3 * MINUTE, Decimal("101.3125")),
    ]


def test_ema_span_one_tracks_prices_immediately() -> None:
    indicator = ExponentialMovingAverage("AAPL", span=1)
    result = run(
        indicator,
        (trade(0, 1, "100.10", "1"), trade(61, 2, "102.00", "1")),
    )
    assert result == [
        (STAMP + MINUTE, Decimal("100.10")),
        (STAMP + 2 * MINUTE, Decimal("102.00")),
    ]


def test_rsi_golden_wilder_period_four() -> None:
    indicator = RelativeStrengthIndex("AAPL", period=4)
    result = run(
        indicator,
        (
            trade(0, 1, "100", "1"),
            trade(30, 2, "101", "1"),
            trade(61, 3, "100", "1"),
            trade(120, 4, "101", "1"),
            trade(181, 5, "100", "1"),
            trade(240, 6, "102", "1"),
        ),
    )
    assert result == [
        (STAMP + 4 * MINUTE, Decimal("50")),
        (STAMP + 5 * MINUTE, Decimal("70")),
    ]


def test_rsi_flat_then_rising_reaches_hundred() -> None:
    indicator = RelativeStrengthIndex("AAPL", period=3)
    result = run(
        indicator,
        (
            trade(0, 1, "100", "1"),
            trade(30, 2, "100", "1"),
            trade(61, 3, "100", "1"),
            trade(120, 4, "100", "1"),
            trade(181, 5, "101", "1"),
        ),
    )
    assert result == [
        (STAMP + 3 * MINUTE, Decimal("100")),
        (STAMP + 4 * MINUTE, Decimal("100")),
    ]


def test_atr_golden_wilder_period_four() -> None:
    indicator = AverageTrueRange("AAPL", period=4)
    result = run(
        indicator,
        (
            trade(0, 1, "100.00", "1"),
            trade(10, 2, "102.00", "1"),
            trade(20, 3, "101.00", "1"),
            trade(61, 4, "103.00", "1"),
            trade(90, 5, "100.00", "1"),
            trade(130, 6, "101.50", "1"),
            trade(190, 7, "102.00", "1"),
            trade(250, 8, "102.50", "1"),
        ),
    )
    assert result == [
        (STAMP + 4 * MINUTE, Decimal("1.75")),
        (STAMP + 5 * MINUTE, Decimal("1.4375")),
    ]


def test_realized_volatility_golden_clean_returns() -> None:
    indicator = RealizedVolatility("AAPL", window="5min")
    result = run(
        indicator,
        (
            trade(0, 1, "100", "1"),
            trade(30, 2, "101", "1"),
            trade(61, 3, "99.99", "1"),
        ),
    )
    assert result == [
        (STAMP + MINUTE, Decimal("0")),
        (STAMP + 2 * MINUTE, Decimal("0.01")),
    ]


def test_realized_volatility_expires_returns_outside_window() -> None:
    indicator = RealizedVolatility("AAPL", window="30s")
    result = run(
        indicator,
        (
            trade(0, 1, "100", "1"),
            trade(45, 2, "101", "1"),
            trade(95, 3, "100", "1"),
            trade(155, 4, "101", "1"),
        ),
    )
    assert result == [
        (STAMP + MINUTE, Decimal("0")),
        (STAMP + 2 * MINUTE, Decimal("0")),
        (STAMP + 3 * MINUTE, Decimal("0")),
    ]


def test_vwap_golden_session_cumulative() -> None:
    indicator = VolumeWeightedAveragePrice("AAPL")
    result = run(
        indicator,
        (
            trade(0, 1, "100", "2"),
            trade(30, 2, "102", "2"),
            trade(61, 3, "104", "2"),
        ),
    )
    assert result == [
        (STAMP + MINUTE, Decimal("101")),
        (STAMP + 2 * MINUTE, Decimal("102")),
    ]


def test_dollar_volume_golden_cumulative() -> None:
    indicator = DollarVolume("AAPL")
    result = run(
        indicator,
        (
            trade(0, 1, "100", "2"),
            trade(30, 2, "102", "2"),
            trade(61, 3, "104", "2"),
        ),
    )
    assert result == [
        (STAMP + MINUTE, Decimal("404")),
        (STAMP + 2 * MINUTE, Decimal("612")),
    ]


def test_dollar_volume_golden_rolling_window() -> None:
    indicator = DollarVolume("AAPL", window="1min")
    result = run(
        indicator,
        (
            trade(0, 1, "100", "2"),
            trade(30, 2, "102", "2"),
            trade(61, 3, "104", "2"),
        ),
    )
    assert result == [
        (STAMP + MINUTE, Decimal("404")),
        (STAMP + 2 * MINUTE, Decimal("208")),
    ]


def test_obv_golden_tick_rule_with_flat_carry() -> None:
    indicator = OnBalanceVolume("AAPL")
    result = run(
        indicator,
        (
            trade(0, 1, "100", "2"),
            trade(30, 2, "101", "1"),
            trade(61, 3, "101", "3"),
            trade(90, 4, "100", "2"),
            trade(130, 5, "100", "1"),
            trade(150, 6, "101", "4"),
        ),
    )
    assert result == [
        (STAMP + MINUTE, Decimal("1")),
        (STAMP + 2 * MINUTE, Decimal("2")),
        (STAMP + 3 * MINUTE, Decimal("5")),
    ]


def test_mid_sma_golden_quote_driven_and_trade_immune() -> None:
    indicator = MidSimpleMovingAverage("AAPL", period=2)
    result = run(
        indicator,
        (
            quote(0, 1, "99.95", "100.05"),
            quote(20, 2, "100.05", "100.15"),
            trade(30, 3, "100.00", "1"),
            quote(61, 4, "99.95", "100.05"),
        ),
    )
    assert result == [
        (STAMP + MINUTE, Decimal("100.05")),
        (STAMP + 2 * MINUTE, Decimal("100.05")),
    ]


def test_mid_sma_warmup_needs_full_window() -> None:
    indicator = MidSimpleMovingAverage("AAPL", period=2)
    assert run(indicator, (quote(0, 1, "99.95", "100.05"),)) == []


def test_vwap_resets_at_midnight_session_boundary() -> None:
    indicator = VolumeWeightedAveragePrice("AAPL")
    day_one = trade(0, 1, "100", "2")
    first_day_two = TradeEvent(
        "t2",
        datetime(2024, 1, 20, 0, 0, 30, tzinfo=UTC),
        2,
        Symbol("AAPL"),
        Decimal("102"),
        Decimal("2"),
    )
    result = run(indicator, (day_one, first_day_two))
    assert result == [
        (STAMP + MINUTE, Decimal("100")),
        (datetime(2024, 1, 20, 0, 1, tzinfo=UTC), Decimal("102")),
    ]


def test_session_boundary_inside_bucket_yields_two_samples() -> None:
    hours_seven = timedelta(hours=7)
    day_two_open = datetime(2024, 1, 20, 0, 0, tzinfo=UTC)
    first_bucket = bucket_start(datetime(2024, 1, 19, 17, 59, tzinfo=UTC), hours_seven)
    shared_bucket = bucket_start(datetime(2024, 1, 19, 23, 0, tzinfo=UTC), hours_seven)
    assert first_bucket < shared_bucket
    assert shared_bucket < day_two_open < shared_bucket + hours_seven

    indicator = VolumeWeightedAveragePrice("AAPL", bucket=hours_seven)
    events = (
        TradeEvent("t1", first_bucket, 1, Symbol("AAPL"), Decimal("100"), Decimal("1")),
        TradeEvent(
            "t2",
            shared_bucket + timedelta(minutes=30),
            2,
            Symbol("AAPL"),
            Decimal("102"),
            Decimal("1"),
        ),
        TradeEvent(
            "t3",
            day_two_open + timedelta(minutes=30),
            3,
            Symbol("AAPL"),
            Decimal("104"),
            Decimal("1"),
        ),
    )
    result = run(indicator, events)
    assert result == [
        (first_bucket + hours_seven, Decimal("100")),
        (shared_bucket + hours_seven, Decimal("101")),
        (shared_bucket + hours_seven, Decimal("104")),
    ]
