"""Behavior of the indicator state machine: ordering, boundaries, sessions,
snapshots, and routing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types import Event, QuoteEvent, TradeEvent
from harness.indicators import (
    IndicatorConfigurationError,
    IndicatorOrderingError,
    IndicatorRouter,
    IndicatorSnapshotError,
    MidSimpleMovingAverage,
    SimpleMovingAverage,
    VolumeWeightedAveragePrice,
    bucket_start,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def trade(offset_seconds: int, sequence: int, price: str, quantity: str) -> TradeEvent:
    return TradeEvent(
        f"t{sequence}",
        STAMP + timedelta(seconds=offset_seconds),
        sequence,
        Symbol("AAPL"),
        Decimal(price),
        Decimal(quantity),
    )


def quote(offset_seconds: int, sequence: int, bid: str, ask: str) -> QuoteEvent:
    return QuoteEvent(
        f"q{sequence}",
        STAMP + timedelta(seconds=offset_seconds),
        sequence,
        Symbol("AAPL"),
        Decimal(bid),
        Decimal("1"),
        Decimal(ask),
        Decimal("1"),
    )


def test_non_market_event_is_rejected() -> None:
    indicator = SimpleMovingAverage("AAPL", period=2)
    with pytest.raises(TypeError, match="QuoteEvent or TradeEvent"):
        indicator.on_event(Event("order-1", STAMP, 1))  # ty: ignore[invalid-argument-type]


def test_foreign_symbol_is_rejected() -> None:
    indicator = SimpleMovingAverage("AAPL", period=2)
    event = TradeEvent("t1", STAMP, 1, Symbol("MSFT"), Decimal("100"), Decimal("1"))
    with pytest.raises(ValueError, match="does not match indicator symbol"):
        indicator.on_event(event)


def test_ordering_regression_is_rejected_across_event_classes() -> None:
    indicator = SimpleMovingAverage("AAPL", period=1)
    indicator.on_event(trade(30, 5, "101", "1"))
    with pytest.raises(IndicatorOrderingError, match="regresses"):
        indicator.on_event(trade(0, 1, "100", "1"))
    with pytest.raises(IndicatorOrderingError, match="regresses"):
        indicator.on_event(quote(10, 6, "99", "101"))


def test_bucket_boundary_is_half_open_on_the_epoch_grid() -> None:
    indicator = SimpleMovingAverage("AAPL", period=1)
    late = TradeEvent(
        "t1",
        STAMP + timedelta(microseconds=59_999_999),
        1,
        Symbol("AAPL"),
        Decimal("100"),
        Decimal("1"),
    )
    exact = TradeEvent(
        "t2", STAMP + MINUTE, 2, Symbol("AAPL"), Decimal("101"), Decimal("1")
    )
    for event in (late, exact):
        indicator.on_event(event)
    indicator.flush()
    samples = indicator.samples()
    assert [(sample.start, sample.end) for sample in samples] == [
        (STAMP, STAMP + MINUTE),
        (STAMP + MINUTE, STAMP + 2 * MINUTE),
    ]
    assert [sample.value for sample in samples] == [Decimal("100"), Decimal("101")]
    assert all(sample.name == "sma" for sample in samples)


def test_quote_rotates_bucket_but_never_feeds_trade_value() -> None:
    indicator = SimpleMovingAverage("AAPL", period=2)
    indicator.on_event(trade(0, 1, "100", "1"))
    indicator.on_event(trade(30, 2, "102", "1"))
    assert indicator.samples() == ()
    indicator.on_event(quote(70, 3, "101", "103"))
    samples = indicator.samples()
    assert len(samples) == 1
    assert samples[0].end == STAMP + MINUTE
    assert samples[0].value == Decimal("101")


def test_quote_only_buckets_emit_nothing_for_trade_indicators() -> None:
    indicator = SimpleMovingAverage("AAPL", period=1)
    indicator.on_event(quote(0, 1, "99", "101"))
    indicator.on_event(quote(70, 2, "100", "102"))
    indicator.flush()
    assert indicator.samples() == ()
    assert indicator.value is None


def test_value_reflects_only_accepted_events() -> None:
    indicator = SimpleMovingAverage("AAPL", period=2)
    assert indicator.value is None
    indicator.on_event(trade(0, 1, "100", "1"))
    assert indicator.value is None
    indicator.on_event(trade(30, 2, "102", "1"))
    assert indicator.value == Decimal("101")
    assert indicator.samples() == ()


def test_flush_is_idempotent_and_emits_the_open_bucket() -> None:
    indicator = SimpleMovingAverage("AAPL", period=1)
    indicator.on_event(trade(0, 1, "100", "1"))
    indicator.flush()
    first = indicator.samples()
    assert len(first) == 1
    indicator.flush()
    assert indicator.samples() == first


def test_snapshot_resume_reproduces_uninterrupted_output() -> None:
    events = (
        trade(0, 1, "100", "1"),
        trade(30, 2, "102", "1"),
        trade(61, 3, "101", "1"),
        trade(120, 4, "103", "1"),
    )
    whole = SimpleMovingAverage("AAPL", period=2)
    for event in events:
        whole.on_event(event)
    whole.flush()

    split = SimpleMovingAverage("AAPL", period=2)
    split.on_event(events[0])
    payload = split.snapshot()
    restored = SimpleMovingAverage("AAPL", period=2)
    restored.restore(payload)
    for event in events[1:]:
        restored.on_event(event)
    restored.flush()

    assert restored.samples() == whole.samples()
    assert restored.value == whole.value
    assert restored.snapshot() == whole.snapshot()


def test_snapshot_payload_is_deterministic_bytes() -> None:
    first = SimpleMovingAverage("AAPL", period=2)
    second = SimpleMovingAverage("AAPL", period=2)
    for indicator in (first, second):
        indicator.on_event(trade(0, 1, "100", "1"))
        indicator.on_event(trade(30, 2, "102", "1"))
    assert first.snapshot() == second.snapshot()


def test_restore_rejects_parameter_mismatch() -> None:
    source = SimpleMovingAverage("AAPL", period=2)
    source.on_event(trade(0, 1, "100", "1"))
    payload = source.snapshot()
    other = SimpleMovingAverage("AAPL", period=3)
    with pytest.raises(IndicatorSnapshotError, match="parameters do not match"):
        other.restore(payload)


def test_restore_rejects_corrupt_payloads() -> None:
    indicator = SimpleMovingAverage("AAPL", period=2)
    with pytest.raises(IndicatorSnapshotError, match="not valid JSON"):
        indicator.restore(b"not json")
    with pytest.raises(IndicatorSnapshotError, match="schema"):
        indicator.restore(b"{}")
    with pytest.raises(IndicatorSnapshotError, match="does not match"):
        indicator.restore(
            b'{"name":"ema","params":{},"schema":"fca-indicator-snapshot-v1",'
            b'"state":{"runtime":{},"indicator":{}}}'
        )


def test_router_routes_by_symbol_and_round_trips_basket_snapshots() -> None:
    apple = SimpleMovingAverage("AAPL", period=2)
    microsoft = SimpleMovingAverage("MSFT", period=2)
    router = IndicatorRouter(apple, microsoft)
    router.on_event(trade(0, 1, "100", "1"))
    router.on_event(
        TradeEvent(
            "t2",
            STAMP + timedelta(seconds=10),
            2,
            Symbol("MSFT"),
            Decimal("200"),
            Decimal("1"),
        )
    )
    router.on_event(trade(30, 3, "102", "1"))
    router.on_event(
        TradeEvent(
            "t4",
            STAMP + timedelta(seconds=40),
            4,
            Symbol("MSFT"),
            Decimal("202"),
            Decimal("1"),
        )
    )
    assert apple.value == Decimal("101")
    assert microsoft.value == Decimal("201")

    payload = router.snapshot()
    fresh_apple = SimpleMovingAverage("AAPL", period=2)
    fresh_microsoft = SimpleMovingAverage("MSFT", period=2)
    fresh = IndicatorRouter(fresh_apple, fresh_microsoft)
    fresh.restore(payload)
    assert fresh.snapshot() == payload

    router.on_event(trade(61, 5, "101", "1"))
    router.flush()
    fresh.on_event(trade(61, 5, "101", "1"))
    fresh.flush()
    assert fresh.samples() == router.samples()
    assert [(sample.end, sample.value) for sample in router.samples()] == [
        (STAMP + MINUTE, Decimal("101")),
        (STAMP + 2 * MINUTE, Decimal("101.5")),
        (STAMP + MINUTE, Decimal("201")),
    ]


def test_router_rejects_duplicate_attachment() -> None:
    indicator = MidSimpleMovingAverage("AAPL", period=2)
    router = IndicatorRouter(indicator)
    with pytest.raises(IndicatorConfigurationError, match="already attached"):
        router.attach(indicator)


def test_bucket_start_matches_kernel_bar_bucketing() -> None:
    interval = timedelta(minutes=5)
    assert bucket_start(STAMP, interval) == STAMP
    assert bucket_start(STAMP + interval, interval) == STAMP + interval
    assert bucket_start(STAMP + interval - timedelta(microseconds=1), interval) == STAMP
    with pytest.raises(ValueError, match="positive"):
        bucket_start(STAMP, timedelta(0))


def test_session_anchor_can_be_disabled() -> None:
    indicator = VolumeWeightedAveragePrice("AAPL", session_anchor=None)
    assert indicator.session_anchor is None
    day_one = trade(0, 1, "100", "2")
    day_two = TradeEvent(
        "t2",
        datetime(2024, 1, 20, 0, 0, 30, tzinfo=UTC),
        2,
        Symbol("AAPL"),
        Decimal("102"),
        Decimal("2"),
    )
    indicator.on_event(day_one)
    indicator.on_event(day_two)
    indicator.flush()
    assert [(sample.end, sample.value) for sample in indicator.samples()] == [
        (STAMP + MINUTE, Decimal("100")),
        (datetime(2024, 1, 20, 0, 1, tzinfo=UTC), Decimal("101")),
    ]
