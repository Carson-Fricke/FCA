from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types.accounting import ExecutionReport, Fill
from harness.core.types.events import Event, QuoteEvent
from harness.core.types.orders import (
    ExecutionType,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.fills.simple_touch import SimpleTouch
from harness.venues.base import ExecutionVenue
from harness.venues.sim import SimVenue, SimVenueError

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")


def quote(
    event_id: str,
    offset: int = 0,
    sequence: int = 0,
    *,
    bid: str = "100",
    ask: str = "101",
    bid_quantity: str = "10",
    ask_quantity: str = "10",
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        AAPL,
        Decimal(bid),
        Decimal(bid_quantity),
        Decimal(ask),
        Decimal(ask_quantity),
    )


def order(
    order_id: str,
    side: Side,
    order_type: OrderType,
    *,
    quantity: str = "10",
    tif: TimeInForce = TimeInForce.DAY,
    limit_price: str | None = None,
    stop_price: str | None = None,
    offset: int = 0,
) -> SingleLegOrder:
    return SingleLegOrder(
        f"event-{order_id}",
        STAMP + timedelta(microseconds=offset),
        0,
        order_id,
        AAPL,
        side,
        Decimal(quantity),
        order_type,
        tif,
        None if limit_price is None else Decimal(limit_price),
        None if stop_price is None else Decimal(stop_price),
    )


def market_and_venue(
    *, latency: timedelta = timedelta(0)
) -> tuple[EventBus, SimVenue, list[Event]]:
    bus = EventBus(SimClock(STAMP))
    venue = SimVenue(bus, SimpleTouch(), latency)
    seen: list[Event] = []
    bus.subscribe(ExecutionReport, seen.append)
    bus.subscribe(Fill, seen.append)
    return bus, venue, seen


def test_buy_and_sell_crossing_scenarios_emit_ack_report_and_fill_via_bus() -> None:
    bus, venue, seen = market_and_venue()
    bus.publish(quote("q", bid="100", ask="101"))
    bus.drain()
    buy = order("buy", Side.BUY, OrderType.LIMIT, limit_price="101")
    sell = order("sell", Side.SELL, OrderType.LIMIT, limit_price="100")

    venue.submit(buy)
    venue.submit(sell)
    assert seen == []
    dispatched = bus.drain()

    reports = [event for event in seen if isinstance(event, ExecutionReport)]
    fills = [event for event in seen if isinstance(event, Fill)]
    assert [report.execution_type for report in reports] == [
        ExecutionType.ACKNOWLEDGED,
        ExecutionType.FILL,
        ExecutionType.ACKNOWLEDGED,
        ExecutionType.FILL,
    ]
    assert [(fill.order_id, fill.side, fill.price) for fill in fills] == [
        ("buy", Side.BUY, Decimal("101")),
        ("sell", Side.SELL, Decimal("100")),
    ]
    assert all(event.timestamp == STAMP for event in dispatched)
    assert isinstance(venue, ExecutionVenue)


@pytest.mark.parametrize(
    ("side", "limit", "bid", "ask", "fill_price"),
    [
        (Side.BUY, "100", "99", "101", Decimal("100")),
        (Side.SELL, "101", "100", "102", Decimal("101")),
    ],
)
def test_resting_orders_fill_only_when_a_later_quote_touches(
    side: Side, limit: str, bid: str, ask: str, fill_price: Decimal
) -> None:
    bus, venue, seen = market_and_venue()
    bus.publish(quote("resting", bid=bid, ask=ask))
    bus.drain()
    venue.submit(order(f"rest-{side.value}", side, OrderType.LIMIT, limit_price=limit))
    bus.drain()
    assert not any(isinstance(event, Fill) for event in seen)

    if side is Side.BUY:
        bus.publish(quote("touch", offset=1, sequence=1, ask="100", bid="99"))
    else:
        bus.publish(quote("touch", offset=1, sequence=1, bid="101", ask="102"))
    bus.drain()

    fills = [event for event in seen if isinstance(event, Fill)]
    assert len(fills) == 1
    assert fills[0].price == fill_price


@pytest.mark.parametrize(
    ("side", "stop", "trigger_bid", "trigger_ask", "fill_price"),
    [
        (Side.BUY, "101", "100", "101", Decimal("101")),
        (Side.SELL, "100", "100", "101", Decimal("100")),
    ],
)
def test_stop_orders_arm_then_trigger_with_snapshot(
    side: Side,
    stop: str,
    trigger_bid: str,
    trigger_ask: str,
    fill_price: Decimal,
) -> None:
    bus, venue, seen = market_and_venue()
    before_bid = "100" if side is Side.BUY else "101"
    before_ask = "101" if side is Side.BUY else "102"
    bus.publish(quote("before", bid=before_bid, ask=before_ask))
    bus.drain()
    stop_order = order(f"stop-{side.value}", side, OrderType.STOP, stop_price=stop)
    venue.submit(stop_order)
    bus.drain()
    assert venue.state(stop_order.order_id).armed
    assert not any(isinstance(event, Fill) for event in seen)

    triggering = quote(
        "trigger",
        offset=1,
        sequence=1,
        bid=trigger_bid,
        ask=trigger_ask,
    )
    bus.publish(triggering)
    bus.drain()

    state = venue.state(stop_order.order_id)
    assert not state.armed
    assert state.triggering_market is triggering
    assert state.decision_price == fill_price
    assert [event.price for event in seen if isinstance(event, Fill)] == [fill_price]


def test_partial_fill_then_worsening_quotes_never_improve_the_fill() -> None:
    bus, venue, seen = market_and_venue()
    venue.submit(
        order("partial", Side.BUY, OrderType.LIMIT, quantity="10", limit_price="101")
    )
    bus.publish(quote("first", sequence=1, ask="101", ask_quantity="3"))
    bus.drain()
    assert [fill.quantity for fill in seen if isinstance(fill, Fill)] == [Decimal("3")]

    bus.publish(quote("worse", offset=1, sequence=2, ask="102", ask_quantity="10"))
    bus.drain()
    assert [fill.quantity for fill in seen if isinstance(fill, Fill)] == [Decimal("3")]

    bus.publish(quote("better", offset=2, sequence=3, ask="100", ask_quantity="7"))
    bus.drain()
    fills = [fill for fill in seen if isinstance(fill, Fill)]
    assert [(fill.quantity, fill.price) for fill in fills] == [
        (Decimal("3"), Decimal("101")),
        (Decimal("7"), Decimal("100")),
    ]


def test_stop_limit_arms_on_trigger_and_can_rest_after_non_crossing_trigger() -> None:
    bus, venue, seen = market_and_venue()
    stop_limit = order(
        "stop-limit",
        Side.BUY,
        OrderType.STOP_LIMIT,
        limit_price="100.50",
        stop_price="101",
    )
    venue.submit(stop_limit)
    trigger = quote("trigger", sequence=1, bid="100", ask="101")
    bus.publish(trigger)
    bus.drain()
    assert venue.state("stop-limit").triggering_market is trigger
    assert not any(isinstance(event, Fill) for event in seen)

    bus.publish(quote("touch", offset=1, sequence=2, ask="100.50"))
    bus.drain()
    assert len([event for event in seen if isinstance(event, Fill)]) == 1


def test_cancellation_and_replacement_outcomes_are_scheduled() -> None:
    bus, venue, seen = market_and_venue()
    venue.submit(order("cancel", Side.BUY, OrderType.LIMIT, limit_price="99"))
    bus.drain()
    venue.cancel("cancel")
    replacement = order(
        "replacement", Side.BUY, OrderType.LIMIT, quantity="4", limit_price="101"
    )
    bus.drain()
    assert venue.state("cancel").status is OrderStatus.CANCELED

    venue.submit(order("original", Side.BUY, OrderType.LIMIT, limit_price="99"))
    bus.drain()
    bus.publish(quote("replacement-market", offset=1, sequence=1, ask="101"))
    bus.drain()
    venue.replace("original", replacement)
    assert venue.state("original").status is OrderStatus.REPLACED
    bus.drain()
    assert venue.state("replacement").status is OrderStatus.FILLED
    assert any(
        isinstance(event, ExecutionReport)
        and event.execution_type is ExecutionType.REPLACED
        for event in seen
    )


def test_latency_preserves_quote_ack_report_fill_ordering_and_timestamps() -> None:
    latency = timedelta(microseconds=25)
    bus, venue, seen = market_and_venue(latency=latency)
    venue.submit(order("delayed", Side.BUY, OrderType.MARKET))
    bus.publish(quote("quote", sequence=1, ask="101"))
    assert seen == []
    dispatched = bus.drain()

    outcome_events = [
        event for event in dispatched if isinstance(event, (ExecutionReport, Fill))
    ]
    assert [type(event) for event in outcome_events] == [
        ExecutionReport,
        ExecutionReport,
        Fill,
    ]
    assert all(event.timestamp == STAMP + latency for event in outcome_events)
    assert [event.event_id for event in seen] == [
        event.event_id for event in outcome_events
    ]
    assert venue.latest_market is not None


def test_ioc_non_fill_expires_through_the_bus() -> None:
    bus, venue, seen = market_and_venue()
    ioc = order(
        "ioc",
        Side.BUY,
        OrderType.LIMIT,
        tif=TimeInForce.IOC,
        limit_price="99",
    )
    venue.submit(ioc)
    bus.publish(quote("opportunity", sequence=1, bid="100", ask="101"))
    bus.drain()

    reports = [event for event in seen if isinstance(event, ExecutionReport)]
    assert [report.status for report in reports] == [
        OrderStatus.ACCEPTED,
        OrderStatus.EXPIRED,
    ]
    assert venue.state("ioc").status is OrderStatus.EXPIRED


def test_commands_do_not_inline_outcomes_or_allow_pending_races() -> None:
    bus, venue, seen = market_and_venue(latency=timedelta(microseconds=1))
    venue.submit(order("pending", Side.BUY, OrderType.MARKET))
    bus.publish(quote("q", sequence=1))
    bus.dispatch_next()
    assert seen == []
    with pytest.raises(SimVenueError, match="pending"):
        venue.cancel("pending")
