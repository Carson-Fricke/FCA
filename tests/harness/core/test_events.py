from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.clock import SimClock
from harness.core.events import EVENT_CLASS_PRIORITY, EventBus, EventQueue
from harness.core.symbols import Symbol
from harness.core.types import Fill, QuoteEvent, Side, TradeEvent

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
ONE = Decimal("1")


def quote(event_id: str, offset: int, sequence: int) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        Symbol("AAPL"),
        Decimal("100"),
        ONE,
        Decimal("101"),
        ONE,
    )


def trade(event_id: str, offset: int, sequence: int) -> TradeEvent:
    return TradeEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        Symbol("AAPL"),
        Decimal("100.5"),
        ONE,
    )


def fill(event_id: str, offset: int, sequence: int) -> Fill:
    return Fill(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        f"fill-{event_id}",
        f"order-{event_id}",
        Symbol("AAPL"),
        Side.BUY,
        ONE,
        Decimal("100.5"),
    )


def test_queue_orders_by_timestamp_sequence_class_then_feed_order() -> None:
    queue = EventQueue(SimClock(STAMP))
    events = (
        fill("fill-priority", 1, 4),
        trade("trade-priority", 1, 4),
        quote("quote-first", 1, 4),
        quote("quote-second", 1, 4),
        quote("later-sequence", 1, 5),
        fill("earlier-sequence", 1, 3),
        trade("earliest-time", 0, 100),
    )
    queue.enqueue_many(events)

    assert (
        EVENT_CLASS_PRIORITY[events[2].event_type]
        < EVENT_CLASS_PRIORITY[events[1].event_type]
    )
    dispatched = []
    while queue:
        event = queue.pop()
        assert event is not None
        dispatched.append(event.event_id)

    assert dispatched == [
        "earliest-time",
        "earlier-sequence",
        "quote-first",
        "quote-second",
        "trade-priority",
        "fill-priority",
        "later-sequence",
    ]


def test_queue_rejects_events_older_than_sim_clock_head() -> None:
    clock = SimClock(STAMP)
    queue = EventQueue(clock)

    with pytest.raises(ValueError, match="older than the clock head"):
        queue.enqueue(quote("stale", -1, 0))

    queue.enqueue(quote("current", 0, 0))
    assert queue.pop() is not None
    clock.advance_by(timedelta(microseconds=2))
    with pytest.raises(ValueError, match="older than the clock head"):
        queue.enqueue(quote("now-stale", 1, 1))


def test_handler_can_schedule_a_future_venue_response() -> None:
    clock = SimClock(STAMP)
    bus = EventBus(clock)
    handled: list[str] = []

    def on_quote(event: QuoteEvent) -> None:
        handled.append(event.event_id)
        bus.schedule(fill("venue-fill", 250, event.sequence))

    bus.subscribe(QuoteEvent, on_quote)
    bus.subscribe(Fill, lambda event: handled.append(event.event_id))
    bus.publish(quote("market-quote", 0, 7))

    assert [event.event_id for event in bus.drain()] == [
        "market-quote",
        "venue-fill",
    ]
    assert handled == ["market-quote", "venue-fill"]
    assert clock.head == STAMP + timedelta(microseconds=250)


def test_scheduled_response_cannot_precede_the_last_dispatch_key() -> None:
    bus = EventBus(SimClock(STAMP))
    bus.publish(trade("market-trade", 0, 7))
    assert bus.dispatch_next() is not None

    with pytest.raises(ValueError, match="ordering key cannot precede"):
        bus.schedule(quote("retrograde-response", 0, 7))


def test_quote_trade_fill_flood_has_stable_dispatch_hash_across_three_runs() -> None:
    hashes = []
    for _ in range(3):
        bus = EventBus(SimClock(STAMP))
        for index in range(10_000):
            offset = index % 29
            sequence = index % 17
            kind = index % 3
            if kind == 0:
                event = quote(f"event-{index}", offset, sequence)
            elif kind == 1:
                event = trade(f"event-{index}", offset, sequence)
            else:
                event = fill(f"event-{index}", offset, sequence)
            bus.publish(event)

        assert len(bus.drain()) == 10_000
        assert bus.dispatched_count == 10_000
        hashes.append(bus.dispatch_hash)

    assert (
        hashes
        == ["949b0dd3fbf95bcb1b93baec06c20e8e5c66ad74e342df01b1e21d9eff769df2"] * 3
    )
