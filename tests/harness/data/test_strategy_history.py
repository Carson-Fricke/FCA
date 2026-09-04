"""Focused tests for the opaque, now-anchored strategy history capability."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from harness.core.clock import Clock, SimClock
from harness.core.symbols import Symbol
from harness.core.types import Event, EventType, QuoteEvent, TradeEvent
from harness.data.history import (
    HistoricalView,
    LookaheadError,
    StrategyHistory,
    to_nanos,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
HEAD = STAMP + timedelta(days=1)
AAPL = Symbol("AAPL")
MSFT = Symbol("MSFT")

FORBIDDEN_SURFACE = (
    "record",
    "record_many",
    "subscribe",
    "attach",
    "clock",
    "head",
    "capacity",
    "subscriptions",
    "symbols",
    "retained",
    "dropped",
    "advance_to",
    "advance_by",
)


class FixedClock:
    """Deterministic clock whose head only test code may move."""

    __slots__ = ("_head",)

    def __init__(self, head: datetime = HEAD) -> None:
        self._head = head

    def now(self) -> datetime:
        return self._head

    def rewind(self, head: datetime) -> None:
        """Move the head backwards; only test code may do this."""
        self._head = head


def quote(
    event_id: str,
    offset_seconds: int = 0,
    sequence: int = 0,
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP + timedelta(seconds=offset_seconds),
        sequence,
        AAPL,
        Decimal("100"),
        Decimal("1"),
        Decimal("101"),
        Decimal("1"),
    )


def trade(
    event_id: str,
    offset_seconds: int = 0,
    sequence: int = 0,
) -> TradeEvent:
    return TradeEvent(
        event_id,
        STAMP + timedelta(seconds=offset_seconds),
        sequence,
        AAPL,
        Decimal("100"),
        Decimal("1"),
    )


def capability_with(
    quotes: int = 0, trades: int = 0
) -> tuple[HistoricalView, FixedClock, StrategyHistory]:
    clock = FixedClock(HEAD)
    view = HistoricalView(clock, capacity=16)
    view.subscribe(AAPL)
    view.record_many(
        [quote(f"q{index}", index, index) for index in range(quotes)]
        + [trade(f"t{index}", index, index) for index in range(trades)]
    )
    return view, clock, StrategyHistory(view, clock)


def test_read_returns_the_same_event_as_the_engine_view_at_the_head() -> None:
    view, clock, capability = capability_with(quotes=3, trades=2)
    head = clock.now()

    assert capability.read(AAPL) is view.read(head, AAPL)
    assert capability.read(AAPL, QuoteEvent) is view.read(head, AAPL, QuoteEvent)
    assert capability.read(AAPL, EventType.TRADE) is view.read(
        head, AAPL, EventType.TRADE
    )
    assert capability.read(MSFT) is None


def test_read_takes_no_timestamp_parameter() -> None:
    assert list(inspect.signature(StrategyHistory.read).parameters) == [
        "self",
        "symbol",
        "data_type",
    ]
    for method in (
        StrategyHistory.read,
        StrategyHistory.last,
        StrategyHistory.window,
        StrategyHistory.aggregate,
        StrategyHistory.bars,
    ):
        assert "as_of" not in inspect.signature(method).parameters


def test_a_future_timestamp_is_unrepresentable_in_a_read() -> None:
    _, _, capability = capability_with(quotes=2)

    with pytest.raises(TypeError, match="symbol must be a string"):
        capability.read(HEAD, AAPL)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="symbol must be a string"):
        capability.read(to_nanos(HEAD) + 1, AAPL)  # ty: ignore[invalid-argument-type]


def test_window_last_aggregate_and_bars_default_to_the_engine_head() -> None:
    view, clock, capability = capability_with(quotes=3, trades=2)
    head = clock.now()
    end = STAMP + timedelta(minutes=5)

    assert capability.window(AAPL, STAMP) == view.window(AAPL, STAMP, head)
    assert capability.window(AAPL, STAMP, end, TradeEvent) == view.window(
        AAPL, STAMP, end, TradeEvent
    )
    assert capability.last(AAPL, 2) == view.last(AAPL, 2, None, head)
    assert capability.last(AAPL, 3, QuoteEvent) == view.last(AAPL, 3, QuoteEvent, head)
    assert capability.aggregate(AAPL, STAMP) == view.aggregate(AAPL, STAMP, head)
    assert capability.bars(AAPL, "1min", STAMP) == view.bars(AAPL, "1min", STAMP, head)


def test_past_bounded_window_queries_remain_available() -> None:
    _, clock, capability = capability_with(quotes=3)

    early = capability.window(AAPL, STAMP, STAMP + timedelta(seconds=2))
    assert tuple(item.event_id for item in early) == ("q0", "q1")
    assert capability.window(AAPL, STAMP) == capability.window(AAPL, STAMP, clock.now())


def test_future_window_bounds_raise_lookahead_error() -> None:
    _, clock, capability = capability_with(quotes=2)
    head = to_nanos(clock.now())

    with pytest.raises(LookaheadError, match="newer than the engine clock"):
        capability.window(AAPL, STAMP, head + 1)
    with pytest.raises(LookaheadError, match="newer than the engine clock"):
        capability.aggregate(AAPL, STAMP, head + 1)
    with pytest.raises(LookaheadError, match="newer than the engine clock"):
        capability.bars(AAPL, "1min", STAMP, head + 1)


def test_reads_return_frozen_snapshots() -> None:
    _, _, capability = capability_with(quotes=1, trades=1)

    newest = capability.read(AAPL)
    assert isinstance(newest, Event)
    field = "symbol"
    with pytest.raises(FrozenInstanceError):
        setattr(newest, field, MSFT)
    window = capability.window(AAPL, STAMP)
    assert isinstance(window, tuple)
    aggregate = capability.aggregate(AAPL, STAMP)
    field = "vwap"
    with pytest.raises(FrozenInstanceError):
        setattr(aggregate, field, None)
    bar = capability.bars(AAPL, "1min", STAMP)[0]
    field = "close"
    with pytest.raises(FrozenInstanceError):
        setattr(bar, field, Decimal("999"))


def test_capability_hides_mutation_management_and_clock_surfaces() -> None:
    _, _, capability = capability_with(quotes=1)

    for name in FORBIDDEN_SURFACE:
        assert not hasattr(capability, name)
        with pytest.raises(AttributeError):
            getattr(capability, name)


def test_capability_rejects_rebinding_and_deletion() -> None:
    _, _, capability = capability_with(quotes=1)
    poison = HistoricalView(FixedClock())

    with pytest.raises(AttributeError):
        setattr(capability, "record", poison.record)  # noqa: B010
    with pytest.raises(AttributeError):
        setattr(capability, "_view", poison)  # noqa: B010
    with pytest.raises(AttributeError):
        delattr(capability, "_view")
    assert not hasattr(capability, "record")


def test_reads_re_stamp_to_the_engine_head_as_the_engine_moves_it() -> None:
    clock = SimClock(STAMP)
    view = HistoricalView(clock, capacity=8)
    view.subscribe(AAPL)
    capability = StrategyHistory(view, clock)

    view.record(quote("q0", 0, 0))
    first = capability.read(AAPL)
    assert isinstance(first, QuoteEvent)
    assert first.event_id == "q0"

    clock.advance_to(STAMP + timedelta(seconds=1))
    view.record(quote("q1", 1, 1))
    second = capability.read(AAPL)
    assert isinstance(second, QuoteEvent)
    assert second.event_id == "q1"

    assert not hasattr(capability, "advance_to")
    with pytest.raises(LookaheadError, match="newer than the engine clock"):
        capability.window(AAPL, STAMP, STAMP + timedelta(days=2))


def test_capability_requires_a_view_and_a_clock() -> None:
    with pytest.raises(TypeError, match="view must be a HistoricalView"):
        StrategyHistory(cast("HistoricalView", object()), FixedClock())
    with pytest.raises(TypeError, match="clock must implement Clock"):
        StrategyHistory(HistoricalView(FixedClock()), cast("Clock", object()))
