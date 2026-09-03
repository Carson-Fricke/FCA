"""Focused tests for the point-in-time history view and its look-ahead guard."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter_ns
from typing import cast

import pytest

import harness.data.history as history_module
from harness.core.clock import Clock
from harness.core.events import EventBus, EventSelector
from harness.core.symbols import Symbol
from harness.core.types import Event, EventType, QuoteEvent, TradeEvent
from harness.data.history import (
    DEFAULT_HISTORY_CAPACITY,
    HistoricalView,
    HistorySubscription,
    LookaheadError,
    from_nanos,
    to_nanos,
)
from harness.portfolio import PortfolioHub
from harness.strategy import Strategy, StrategyContext
from harness.strategy.binding import StrategyBinding

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
HEAD = STAMP + timedelta(days=1)
AAPL = Symbol("AAPL")
MSFT = Symbol("MSFT")


class FixedClock:
    """Deterministic, non-advanceable clock that freezes the engine head."""

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
    *,
    bid: str = "100",
    ask: str = "101",
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP + timedelta(seconds=offset_seconds),
        sequence,
        AAPL,
        Decimal(bid),
        Decimal("1"),
        Decimal(ask),
        Decimal("1"),
    )


def trade(
    event_id: str,
    offset_seconds: int = 0,
    sequence: int = 0,
    *,
    price: str = "100",
    quantity: str = "1",
) -> TradeEvent:
    return TradeEvent(
        event_id,
        STAMP + timedelta(seconds=offset_seconds),
        sequence,
        AAPL,
        Decimal(price),
        Decimal(quantity),
    )


def filled(
    capacity: int = DEFAULT_HISTORY_CAPACITY, quotes: int = 0, trades: int = 0
) -> HistoricalView:
    view = HistoricalView(FixedClock(), capacity=capacity)
    view.subscribe(AAPL)
    view.record_many(
        [quote(f"q{index}", index, index) for index in range(quotes)]
        + [trade(f"t{index}", index, index) for index in range(trades)]
    )
    return view


def test_point_in_time_reads_stop_at_the_requested_instant() -> None:
    view = filled(quotes=3, trades=2)

    def quote_id(as_of: datetime | int | None = None) -> str:
        event = view.quote(AAPL, as_of)
        assert event is not None
        return event.event_id

    def trade_id(as_of: datetime | int | None = None) -> str:
        event = view.trade(AAPL, as_of)
        assert event is not None
        return event.event_id

    assert quote_id(STAMP + timedelta(seconds=1)) == "q1"
    assert quote_id(STAMP + timedelta(seconds=2)) == "q2"
    # One nanosecond before the second quote must not see it.
    assert quote_id(to_nanos(STAMP + timedelta(seconds=2)) - 1) == "q1"
    assert quote_id() == "q2"
    assert trade_id() == "t1"
    assert view.quote(MSFT) is None


def test_one_nanosecond_future_query_raises_lookahead_error() -> None:
    clock = FixedClock()
    view = HistoricalView(clock, capacity=8)
    view.subscribe(AAPL)
    view.record_many([quote("q0", 0, 0), trade("t0", 0, 0)])
    head = to_nanos(clock.now())

    assert view.quote(AAPL, head) is not None
    for query in (
        lambda: view.quote(AAPL, head + 1),
        lambda: view.trade(AAPL, head + 1),
        lambda: view.last_quotes(AAPL, 2, head + 1),
        lambda: view.last_trades(AAPL, 2, head + 1),
        lambda: view.last(AAPL, 2, as_of=head + 1),
        lambda: view.quotes(AAPL, STAMP, head + 1),
        lambda: view.trades(AAPL, STAMP, head + 1),
        lambda: view.window(AAPL, STAMP, head + 1),
        lambda: view.aggregate(AAPL, STAMP, head + 1),
        lambda: view.bars(AAPL, "1min", end=head + 1),
    ):
        with pytest.raises(LookaheadError, match="newer than the engine clock"):
            query()

    with pytest.raises(LookaheadError, match="newer than the engine clock"):
        view.read(head + 1, AAPL)


def test_recording_a_future_event_raises_lookahead_error() -> None:
    view = HistoricalView(FixedClock(), capacity=4)
    view.subscribe(AAPL)

    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        view.record(quote("future", 3 * 86_400))


def test_ring_buffers_stay_bounded_and_keep_the_newest_events() -> None:
    view = filled(capacity=4, quotes=10)

    assert view.retained(AAPL, EventType.QUOTE) == 4
    assert view.dropped(AAPL, EventType.QUOTE) == 6
    assert tuple(item.event_id for item in view.last_quotes(AAPL, 4)) == (
        "q6",
        "q7",
        "q8",
        "q9",
    )
    assert tuple(item.event_id for item in view.last_quotes(AAPL, 1000)) == (
        "q6",
        "q7",
        "q8",
        "q9",
    )
    newest = view.quote(AAPL)
    assert newest is not None
    assert newest.event_id == "q9"


def test_last_merges_quotes_and_trades_in_deterministic_order() -> None:
    view = HistoricalView(FixedClock(), capacity=8)
    view.subscribe(AAPL)
    view.record_many(
        [
            quote("q0", 0, 0),
            trade("t0", 0, 1),
            quote("q1", 1, 2),
            trade("t1", 1, 3),
        ]
    )

    assert tuple(event.event_id for event in view.last(AAPL, 4)) == (
        "q0",
        "t0",
        "q1",
        "t1",
    )
    assert tuple(event.event_id for event in view.last(AAPL, 2)) == ("q1", "t1")
    assert tuple(event.event_id for event in view.last(AAPL, 3, TradeEvent)) == (
        "t0",
        "t1",
    )
    assert tuple(event.event_id for event in view.last(AAPL, 0)) == ()


def test_window_aggregate_and_bars_reads_reconcile_exactly() -> None:
    view = HistoricalView(FixedClock(), capacity=16)
    view.subscribe(AAPL)
    view.record_many(
        [
            trade("t0", 0, 0, price="100", quantity="1"),
            trade("t1", 30, 1, price="101", quantity="2"),
            quote("q0", 45, 2, bid="99.5", ask="101.5"),
            trade("t2", 61, 3, price="102", quantity="3"),
        ]
    )
    end = STAMP + timedelta(minutes=5)

    assert tuple(event.event_id for event in view.window(AAPL, STAMP, end)) == (
        "t0",
        "t1",
        "q0",
        "t2",
    )
    assert tuple(item.event_id for item in view.trades(AAPL, STAMP, end)) == (
        "t0",
        "t1",
        "t2",
    )
    assert (
        tuple(
            item.event_id
            for item in view.quotes(AAPL, STAMP, STAMP + timedelta(seconds=45))
        )
        == ()
    )

    aggregate = view.aggregate(AAPL, STAMP, end)
    assert aggregate.quote_count == 1
    assert aggregate.trade_count == 3
    assert aggregate.volume == Decimal("6")
    assert aggregate.notional == Decimal("608")
    assert aggregate.vwap == Decimal("608") / Decimal("6")
    assert (aggregate.high, aggregate.low) == (Decimal("102"), Decimal("100"))
    assert (aggregate.first_price, aggregate.last_price) == (
        Decimal("100"),
        Decimal("102"),
    )
    assert (aggregate.last_bid, aggregate.last_ask) == (
        Decimal("99.5"),
        Decimal("101.5"),
    )

    bars = view.bars(AAPL, "1min", STAMP, end)
    assert [(bar.open, bar.close, bar.volume, bar.trade_count) for bar in bars] == [
        (Decimal("100"), Decimal("101"), Decimal("3"), 2),
        (Decimal("102"), Decimal("102"), Decimal("3"), 1),
    ]
    with pytest.raises(ValueError, match="end must be after start"):
        view.aggregate(AAPL, STAMP, STAMP)


def test_read_implements_the_point_in_time_data_boundary() -> None:
    view = filled(quotes=2, trades=2)

    def read(
        stamp: datetime,
        symbol: Symbol = AAPL,
        data_type: type[Event] | EventType | None = None,
    ) -> QuoteEvent | TradeEvent | None:
        return cast(
            "QuoteEvent | TradeEvent | None", view.read(stamp, symbol, data_type)
        )

    latest = read(HEAD)
    quote_only = read(HEAD, AAPL, QuoteEvent)
    trade_only = read(HEAD, AAPL, EventType.TRADE)
    assert latest is not None and latest.event_id == "t1"
    assert quote_only is not None and quote_only.event_id == "q1"
    assert trade_only is not None and trade_only.event_id == "t1"
    assert read(HEAD, MSFT) is None
    assert read(STAMP - timedelta(seconds=1), AAPL) is None


class LookaheadCanaryStrategy(Strategy):
    """Records every observed event and every point-in-time read it obtains."""

    _context: StrategyContext

    def __init__(self, history: HistoricalView) -> None:
        self._history = history
        self.observed: list[str] = []
        self.reads: list[str] = []
        self.violations: list[str] = []
        self.lookahead_blocked = 0

    def on_start(self, context: StrategyContext) -> None:
        self._context = context

    def on_quote(self, event: QuoteEvent) -> None:
        self._observe(event)

    def on_trade(self, event: TradeEvent) -> None:
        self._observe(event)

    def _observe(self, event: QuoteEvent | TradeEvent) -> None:
        context = self._context
        history = self._history
        head = context.clock.now()
        self.observed.append(event.event_id)
        read = context.data.read(head, event.symbol) if context.data else None
        if isinstance(read, QuoteEvent | TradeEvent):
            self.reads.append(read.event_id)
            if read.timestamp > head:
                self.violations.append(f"{event.event_id}:future:{read.event_id}")
        try:
            history.last_quotes(event.symbol, 1, to_nanos(head) + 1)
        except LookaheadError:
            self.lookahead_blocked += 1
        else:
            self.violations.append(f"{event.event_id}:future-query-allowed")


def test_canary_strategy_never_sees_a_future_event() -> None:
    clock = FixedClock(STAMP)
    bus = EventBus(clock)
    view = HistoricalView(clock, capacity=16)
    view.subscribe(AAPL, Event)
    view.attach(bus)
    hub = PortfolioHub("account")
    hub.create_book("strategy", "1000")
    context = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        data=view,
        universe=(AAPL,),
        subscriptions=(QuoteEvent, TradeEvent),
        book_id="strategy",
        seed=1729,
    )
    canary = LookaheadCanaryStrategy(view)
    StrategyBinding(canary, context).start()
    dispatched = [quote("q0", 0, 0), quote("q1", 0, 1), trade("t0", 0, 2)]
    future = [quote("future-0", 30, 3), quote("future-1", 60, 4)]
    bus.publish_many(dispatched + future)

    bus.drain()

    assert canary.observed == ["q0", "q1", "t0"]
    assert canary.reads == ["q0", "q1", "t0"]
    assert canary.violations == []
    assert canary.lookahead_blocked == 3
    assert len(bus) == len(future)
    assert tuple(item.event_id for item in view.last_quotes(AAPL, 100)) == ("q0", "q1")
    retained = {event.event_id for event in view.last(AAPL, 100)}
    assert retained == {"q0", "q1", "t0"}
    assert retained.isdisjoint({event.event_id for event in future})


def test_history_module_uses_no_wall_clock_random_or_network_source() -> None:
    source = inspect.getsource(history_module)
    forbidden = (
        "datetime.now",
        "utcnow",
        "time.time",
        "monotonic",
        "perf_counter",
        "import random",
        "random.",
        "secrets",
        "socket",
        "urllib",
        "httpx",
        "requests",
        "open(",
    )
    offenders = tuple(token for token in forbidden if token in source)

    assert offenders == ()
    assert not any(
        name.startswith(("urllib", "socket", "http")) for name in dir(history_module)
    )


def test_queries_hide_events_newer_than_the_engine_head() -> None:
    clock = FixedClock()
    view = HistoricalView(clock, capacity=8)
    view.subscribe(AAPL)
    view.record_many([quote("past", 0, 0), quote("future", 86_400, 1)])
    head = STAMP + timedelta(seconds=1)
    clock.rewind(head)

    assert tuple(item.event_id for item in view.last_quotes(AAPL, 10)) == ("past",)
    assert tuple(item.event_id for item in view.window(AAPL, STAMP, head)) == ("past",)
    assert view.aggregate(AAPL, STAMP, head).quote_count == 1
    newest = view.quote(AAPL)
    assert newest is not None
    assert newest.event_id == "past"
    assert view.read(head, AAPL) is not None
    with pytest.raises(LookaheadError):
        view.quote(AAPL, HEAD)


def test_bus_attachment_records_each_event_once() -> None:
    clock = FixedClock(STAMP)
    bus = EventBus(clock)
    view = HistoricalView(clock, capacity=16, bus=bus)
    view.subscribe(AAPL, Event)
    view.attach(bus)

    bus.publish_many([quote("q0", 0, 0), trade("t0", 0, 1)])
    bus.drain()

    assert view.retained(AAPL) == 2


def test_subscriptions_validate_selector_capacity_and_clock() -> None:
    view = HistoricalView(FixedClock(), capacity=2)

    assert view.subscribe(AAPL).capacity == 2
    assert view.subscribe(MSFT, EventType.TRADE, capacity=8).capacity == 8
    assert view.subscribe(AAPL).capacity == 2
    assert view.symbols == (AAPL, MSFT)
    assert (
        HistoricalView(
            FixedClock(), subscriptions=[HistorySubscription(AAPL, Event, 4)]
        ).retained(AAPL)
        == 0
    )
    with pytest.raises(ValueError, match="quote and trade events only"):
        view.subscribe(AAPL, EventType.ORDER)
    with pytest.raises(ValueError, match="capacity must be a positive integer"):
        view.subscribe(AAPL, capacity=0)
    with pytest.raises(TypeError, match="selector must be an Event class"):
        HistorySubscription(AAPL, cast("EventSelector", "quote"), 4)
    with pytest.raises(TypeError, match="clock must implement Clock"):
        HistoricalView(cast("Clock", object()))


def test_invalid_queries_raise_before_touching_history() -> None:
    view = filled(quotes=2)

    with pytest.raises(ValueError, match="end must not be before start"):
        view.quotes(AAPL, STAMP + timedelta(seconds=1), STAMP)
    with pytest.raises(ValueError, match="count must be a non-negative integer"):
        view.last(AAPL, -1)
    with pytest.raises(ValueError, match="must be timezone-aware"):
        view.quote(AAPL, datetime(2024, 1, 19, 14, 30))
    with pytest.raises(ValueError, match="quote and trade events only"):
        view.record(Event("e0", STAMP, 0))
    with pytest.raises(ValueError, match="ordering key cannot precede"):
        view.record(quote("regression", 0, 0))


def test_nanosecond_conversion_round_trips_microsecond_timestamps() -> None:
    stamp = datetime(2024, 1, 19, 14, 30, 15, 250_000, tzinfo=UTC)

    assert to_nanos(stamp) == 1_705_674_615_250_000_000
    assert from_nanos(to_nanos(stamp)) == stamp
    assert from_nanos(to_nanos(stamp) + 999) == stamp
    assert to_nanos(to_nanos(stamp)) == to_nanos(stamp)
    with pytest.raises(TypeError):
        to_nanos(cast("datetime", "2024-01-19"))


def test_last_one_thousand_quotes_is_amortized_constant_time() -> None:
    small = HistoricalView(FixedClock(), capacity=1_000)
    small.subscribe(AAPL, EventType.QUOTE, capacity=1_000)
    small.record_many(quote(f"q{index}", index, index) for index in range(1_000))
    large = HistoricalView(FixedClock(), capacity=20_000)
    large.subscribe(AAPL, EventType.QUOTE, capacity=20_000)
    large.record_many(quote(f"q{index}", index, index) for index in range(20_000))

    for view in (small, large):
        for _ in range(3):
            assert len(view.last_quotes(AAPL, 1_000)) == 1_000

    def best(view: HistoricalView) -> int:
        samples = []
        for _ in range(7):
            start = perf_counter_ns()
            result = view.last_quotes(AAPL, 1_000)
            samples.append(perf_counter_ns() - start)
        assert len(result) == 1_000
        return min(samples)

    small_best = best(small)
    large_best = best(large)
    assert tuple(item.event_id for item in small.last_quotes(AAPL, 1_000)) == tuple(
        f"q{index}" for index in range(1_000)
    )
    assert tuple(item.event_id for item in large.last_quotes(AAPL, 1_000)) == tuple(
        f"q{index}" for index in range(19_000, 20_000)
    )
    assert large.retained(AAPL, EventType.QUOTE) == 20_000
    # Twenty times the retained history must not cost twenty times the read.
    # The generous slack keeps scheduler noise out of the assertion.
    assert large_best < 20 * small_best + 500_000
