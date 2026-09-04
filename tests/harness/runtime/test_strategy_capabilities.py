"""Unit tests for the strategy-facing venue and scheduler capabilities."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types import (
    AccountSnapshot,
    Book,
    Event,
    ExecutionReport,
    ExecutionType,
    Fill,
    OrderRequest,
    OrderStatus,
    Position,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TradeEvent,
)
from harness.runtime.backtest.strategies import (
    _RESERVED_EVENT_CLASSES,
    StrategyScheduler,
    StrategyVenue,
)
from harness.strategy import Scheduler
from harness.venues import ExecutionVenue

STAMP = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)
SPY = Symbol("SPY")

FORBIDDEN_VENUE_STATE = (
    "remaining_steps",
    "calls",
    "state",
    "decisions_for",
    "active_orders",
    "latest_market",
    "steps",
)


class RecordingVenue:
    """Structural execution venue recording commands without outcomes."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def submit(self, order: OrderRequest) -> None:
        self.commands.append(("submit", order.order_id))

    def cancel(self, order_id: str) -> None:
        self.commands.append(("cancel", order_id))

    def replace(self, order_id: str, replacement: OrderRequest) -> None:
        self.commands.append(("replace", order_id, replacement.order_id))

    def request_position_snapshot(self, account_id: str) -> None:
        self.commands.append(("position_snapshot", account_id))


def _order(order_id: str) -> SingleLegOrder:
    return SingleLegOrder(
        event_id=order_id,
        timestamp=STAMP,
        sequence=0,
        order_id=order_id,
        symbol=SPY,
        side=Side.BUY,
        quantity=Decimal("1"),
    )


def test_venue_capability_forwards_all_four_commands() -> None:
    venue = RecordingVenue()
    capability = StrategyVenue(venue)

    capability.submit(_order("order-1"))
    capability.cancel("order-1")
    capability.replace("order-1", _order("order-2"))
    capability.request_position_snapshot("account-1")

    assert venue.commands == [
        ("submit", "order-1"),
        ("cancel", "order-1"),
        ("replace", "order-1", "order-2"),
        ("position_snapshot", "account-1"),
    ]


def test_venue_capability_exposes_no_venue_state() -> None:
    capability = StrategyVenue(RecordingVenue())

    for attribute in FORBIDDEN_VENUE_STATE:
        assert not hasattr(capability, attribute)
    assert not set(FORBIDDEN_VENUE_STATE).intersection(dir(capability))


def test_venue_capability_satisfies_the_execution_venue_protocol() -> None:
    assert isinstance(StrategyVenue(RecordingVenue()), ExecutionVenue)


def test_venue_capability_rejects_incomplete_venue_objects() -> None:
    class PartialVenue:
        def submit(self, order: OrderRequest) -> None: ...

    with pytest.raises(TypeError, match="ExecutionVenue"):
        StrategyVenue(cast("ExecutionVenue", PartialVenue()))


def test_scheduler_forwards_strategy_owned_events() -> None:
    bus = EventBus(SimClock(STAMP))
    scheduler = StrategyScheduler(bus)
    note = Event(event_id="note-1", timestamp=STAMP, sequence=0)
    order = SingleLegOrder(
        event_id="order-1",
        timestamp=STAMP,
        sequence=1,
        order_id="order-1",
        symbol=SPY,
        side=Side.SELL,
        quantity=Decimal("3"),
    )

    scheduler.schedule(note)
    scheduler.schedule(order)

    assert len(bus) == 2


def test_scheduler_refuses_reserved_feed_and_outcome_events() -> None:
    bus = EventBus(SimClock(STAMP))
    scheduler = StrategyScheduler(bus)

    for event in _reserved_event_samples():
        with pytest.raises(TypeError, match="reserved"):
            scheduler.schedule(event)

    assert len(bus) == 0


def test_reserved_classes_match_the_refused_samples() -> None:
    samples = _reserved_event_samples()

    assert tuple(type(sample) for sample in samples) == _RESERVED_EVENT_CLASSES


def test_scheduler_satisfies_the_scheduler_protocol() -> None:
    assert isinstance(StrategyScheduler(EventBus(SimClock(STAMP))), Scheduler)


def test_scheduler_requires_an_event_bus() -> None:
    with pytest.raises(TypeError, match="EventBus"):
        StrategyScheduler(cast("EventBus", "not-a-bus"))


def _reserved_event_samples() -> tuple[Event, ...]:
    return (
        QuoteEvent(
            event_id="quote",
            timestamp=STAMP,
            sequence=0,
            symbol=SPY,
            bid_price=Decimal("100"),
            bid_quantity=Decimal("1"),
            ask_price=Decimal("101"),
            ask_quantity=Decimal("1"),
        ),
        TradeEvent(
            event_id="trade",
            timestamp=STAMP,
            sequence=0,
            symbol=SPY,
            price=Decimal("100"),
            quantity=Decimal("1"),
        ),
        ExecutionReport(
            event_id="report",
            timestamp=STAMP,
            sequence=0,
            report_id="report-1",
            order_id="order-1",
            status=OrderStatus.NEW,
            execution_type=ExecutionType.ACKNOWLEDGED,
            order_quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
        ),
        Fill(
            event_id="fill",
            timestamp=STAMP,
            sequence=0,
            fill_id="fill-1",
            order_id="order-1",
            symbol=SPY,
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
        ),
        Position(
            event_id="position",
            timestamp=STAMP,
            sequence=0,
            account_id="account-1",
            symbol=SPY,
            quantity=Decimal("1"),
            average_price=Decimal("100"),
        ),
        Book(event_id="book", timestamp=STAMP, sequence=0, symbol=SPY),
        AccountSnapshot(
            event_id="account",
            timestamp=STAMP,
            sequence=0,
            account_id="account-1",
            cash=Decimal("100"),
            equity=Decimal("100"),
            buying_power=Decimal("100"),
        ),
    )
