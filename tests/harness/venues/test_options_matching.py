from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types.accounting import Fill
from harness.core.types.events import QuoteEvent
from harness.core.types.orders import (
    MultiLegOrder,
    OrderLeg,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.fills.mid_touch import MidTouch
from harness.fills.simple_touch import SimpleTouch
from harness.oms.groups import (
    GroupOrderType,
    GroupStatus,
    GroupTransitionError,
    MultiLegOMS,
)
from harness.venues.options_matching import (
    DuplicateOptionsGroupError,
    OptionsMatchingEngine,
    UnknownOptionsGroupError,
)
from harness.venues.sim import SimVenue

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
LONG_CALL = Symbol("AAPL240119C00100000")
SHORT_CALL = Symbol("AAPL240119C00105000")
UNRELATED = Symbol("MSFT240119C00200000")


def quote(
    symbol: Symbol,
    event_id: str,
    offset: int = 0,
    *,
    bid: str,
    ask: str,
    bid_quantity: str = "5",
    ask_quantity: str = "5",
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        offset,
        symbol,
        Decimal(bid),
        Decimal(bid_quantity),
        Decimal(ask),
        Decimal(ask_quantity),
    )


def vertical(
    order_id: str = "vertical",
    *,
    order_type: OrderType = OrderType.LIMIT,
    limit_price: str | None = "2.10",
    time_in_force: TimeInForce = TimeInForce.DAY,
    quantity: str = "2",
) -> MultiLegOrder:
    return MultiLegOrder(
        event_id=f"event-{order_id}",
        timestamp=STAMP,
        sequence=0,
        order_id=order_id,
        legs=(
            OrderLeg(LONG_CALL, Side.BUY, Decimal(quantity), leg_id="long"),
            OrderLeg(SHORT_CALL, Side.SELL, Decimal(quantity), leg_id="short"),
        ),
        order_type=order_type,
        time_in_force=time_in_force,
        limit_price=None if limit_price is None else Decimal(limit_price),
    )


def complete_quotes() -> tuple[QuoteEvent, QuoteEvent]:
    return (
        quote(LONG_CALL, "long-quote", 1, bid="3.95", ask="4.05"),
        quote(SHORT_CALL, "short-quote", 2, bid="1.95", ask="2.05"),
    )


def engine_with_bus(
    *,
    latency: timedelta = timedelta(0),
    oms: MultiLegOMS | None = None,
    venue: str = "options",
    model: MidTouch | None = None,
) -> tuple[EventBus, OptionsMatchingEngine, list[Fill]]:
    bus = EventBus(SimClock(STAMP))
    engine = OptionsMatchingEngine(bus, model, latency, venue=venue, oms=oms)
    fills: list[Fill] = []
    bus.subscribe(Fill, fills.append)
    return bus, engine, fills


def test_limit_vertical_fills_atomically_on_one_constituent_update() -> None:
    bus, engine, fills = engine_with_bus()
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    bus.drain()
    assert fills == []
    assert engine.state("vertical").status is GroupStatus.ACCEPTED

    for quote_event in complete_quotes():
        bus.publish(quote_event)
    bus.drain()

    long_fill, short_fill = fills
    assert long_fill.order_id == "vertical"
    assert long_fill.symbol == LONG_CALL
    assert long_fill.side is Side.BUY
    assert long_fill.quantity == Decimal("2")
    assert long_fill.price == Decimal("4.00")
    assert long_fill.liquidity == "maker"
    assert short_fill.price == Decimal("2.00")
    assert short_fill.side is Side.SELL
    snapshot = engine.state("vertical")
    assert snapshot.status is GroupStatus.FILLED
    assert snapshot.net_price == Decimal("2.00")
    assert snapshot.gross_cash == Decimal("-4.00")
    assert engine.fills_for("vertical") == (long_fill, short_fill)


def test_staggered_quotes_never_partially_fill_a_group() -> None:
    bus, engine, fills = engine_with_bus()
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    bus.drain()

    bus.publish(complete_quotes()[0])
    bus.drain()
    assert fills == []
    assert engine.state("vertical").status is GroupStatus.ACCEPTED

    bus.publish(complete_quotes()[1])
    bus.drain()
    assert len(fills) == 2
    assert {fill.symbol for fill in fills} == {LONG_CALL, SHORT_CALL}
    assert all(fill.timestamp == STAMP + timedelta(microseconds=2) for fill in fills)


@pytest.mark.parametrize("short_leg_size", ["1", "2"])
def test_non_fillable_leg_blocks_the_whole_group(short_leg_size: str) -> None:
    bus, engine, fills = engine_with_bus()
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    bus.publish(quote(LONG_CALL, "long-quote", 1, bid="3.95", ask="4.05"))
    bus.publish(
        quote(
            SHORT_CALL,
            "short-thin",
            2,
            bid="1.95",
            ask="2.05",
            bid_quantity=short_leg_size,
        )
    )
    bus.drain()

    if Decimal(short_leg_size) < Decimal("2"):
        assert fills == []
        assert engine.state("vertical").status is GroupStatus.ACCEPTED

        bus.publish(
            quote(
                SHORT_CALL,
                "short-fixed",
                3,
                bid="1.95",
                ask="2.05",
                bid_quantity="5",
            )
        )
        bus.drain()
    assert len(fills) == 2


def test_non_constituent_quote_does_not_trigger_evaluation() -> None:
    bus, engine, fills = engine_with_bus()
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    bus.publish(complete_quotes()[0])
    bus.publish(quote(UNRELATED, "noise", 5, bid="9.00", ask="9.10"))
    bus.drain()

    assert fills == []
    assert engine.latest_quote(UNRELATED) is not None
    assert engine.state("vertical").status is GroupStatus.ACCEPTED


@pytest.mark.parametrize(
    ("limit_price", "long_quote", "short_quote", "fills_expected"),
    [
        ("2.10", ("3.95", "4.05"), ("1.95", "2.05"), True),
        ("2.00", ("3.95", "4.05"), ("1.95", "2.05"), False),
        ("5.00", ("3.95", "4.05"), ("4.95", "5.05"), False),
    ],
)
def test_net_debit_limit_scenario_matrix(
    limit_price: str,
    long_quote: tuple[str, str],
    short_quote: tuple[str, str],
    fills_expected: bool,
) -> None:
    bus, engine, fills = engine_with_bus()
    engine.submit(vertical(limit_price=limit_price), GroupOrderType.NET_DEBIT)
    bus.publish(quote(LONG_CALL, "long", 1, bid=long_quote[0], ask=long_quote[1]))
    bus.publish(quote(SHORT_CALL, "short", 2, bid=short_quote[0], ask=short_quote[1]))
    bus.drain()

    assert (len(fills) == 2) is fills_expected
    assert engine.state("vertical").status is (
        GroupStatus.FILLED if fills_expected else GroupStatus.ACCEPTED
    )


@pytest.mark.parametrize(
    ("limit_price", "long_quote", "short_quote", "fills_expected"),
    [
        ("0.90", ("4.00", "4.10"), ("3.00", "3.10"), True),
        ("0.50", ("4.00", "4.10"), ("3.00", "3.10"), True),
        ("1.00", ("4.00", "4.10"), ("3.00", "3.10"), False),
        ("0.90", ("3.00", "3.00"), ("5.00", "5.00"), False),
    ],
)
def test_net_credit_limit_scenario_matrix(
    limit_price: str,
    long_quote: tuple[str, str],
    short_quote: tuple[str, str],
    fills_expected: bool,
) -> None:
    bus, engine, fills = engine_with_bus()
    credit = MultiLegOrder(
        event_id="event-credit",
        timestamp=STAMP,
        sequence=0,
        order_id="credit",
        legs=(
            OrderLeg(LONG_CALL, Side.SELL, Decimal("2"), leg_id="long"),
            OrderLeg(SHORT_CALL, Side.BUY, Decimal("2"), leg_id="short"),
        ),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(limit_price),
    )
    engine.submit(credit, GroupOrderType.NET_CREDIT)
    bus.publish(quote(LONG_CALL, "long", 1, bid=long_quote[0], ask=long_quote[1]))
    bus.publish(quote(SHORT_CALL, "short", 2, bid=short_quote[0], ask=short_quote[1]))
    bus.drain()

    assert (len(fills) == 2) is fills_expected
    if fills_expected:
        long_fill, short_fill = fills
        assert long_fill.price == Decimal("4.05")
        assert short_fill.price == Decimal("3.05")
        assert engine.state("credit").net_price == Decimal("-1.00")


def test_market_group_fills_every_leg_at_the_far_touch() -> None:
    bus, engine, fills = engine_with_bus()
    engine.submit(vertical(order_type=OrderType.MARKET, limit_price=None))

    for quote_event in complete_quotes():
        bus.publish(quote_event)
    bus.drain()

    long_fill, short_fill = fills
    assert long_fill.price == Decimal("4.05")
    assert short_fill.price == Decimal("1.95")
    assert long_fill.liquidity == "taker"
    assert short_fill.liquidity == "taker"
    assert engine.state("vertical").status is GroupStatus.FILLED


def test_ratio_legs_fill_full_leg_quantities() -> None:
    bus, engine, fills = engine_with_bus()
    ratio_order = MultiLegOrder(
        event_id="event-ratio",
        timestamp=STAMP,
        sequence=0,
        order_id="ratio",
        legs=(
            OrderLeg(
                LONG_CALL,
                Side.BUY,
                Decimal("2"),
                ratio=Decimal("2"),
                leg_id="long",
            ),
            OrderLeg(SHORT_CALL, Side.SELL, Decimal("1"), leg_id="short"),
        ),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("7.00"),
    )
    engine.submit(ratio_order, GroupOrderType.NET_DEBIT)
    for quote_event in complete_quotes():
        bus.publish(quote_event)
    bus.drain()

    long_fill, short_fill = fills
    assert long_fill.quantity == Decimal("2")
    assert short_fill.quantity == Decimal("1")
    snapshot = engine.state("ratio")
    assert snapshot.quantity == Decimal("1")
    assert snapshot.net_price == Decimal("6.00")


def test_ioc_group_expires_without_an_immediate_fill() -> None:
    bus, engine, fills = engine_with_bus()
    engine.submit(vertical(time_in_force=TimeInForce.IOC), GroupOrderType.NET_DEBIT)
    bus.drain()

    assert engine.state("vertical").status is GroupStatus.EXPIRED
    for quote_event in complete_quotes():
        bus.publish(quote_event)
    bus.drain()
    assert fills == []


def test_ioc_group_fills_when_immediately_fillable() -> None:
    bus, engine, fills = engine_with_bus()
    for quote_event in complete_quotes():
        bus.publish(quote_event)
    bus.drain()

    engine.submit(vertical(time_in_force=TimeInForce.IOC), GroupOrderType.NET_DEBIT)
    bus.drain()
    assert len(fills) == 2
    assert engine.state("vertical").status is GroupStatus.FILLED


def test_cancel_blocks_later_quotes() -> None:
    bus, engine, fills = engine_with_bus()
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    engine.cancel("vertical")
    bus.drain()
    assert engine.state("vertical").status is GroupStatus.CANCELED

    for quote_event in complete_quotes():
        bus.publish(quote_event)
    bus.drain()
    assert fills == []


def test_filled_group_cannot_be_canceled() -> None:
    bus, engine, _ = engine_with_bus()
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    for quote_event in complete_quotes():
        bus.publish(quote_event)
    bus.drain()
    assert engine.state("vertical").status is GroupStatus.FILLED

    with pytest.raises(GroupTransitionError):
        engine.cancel("vertical")


def test_duplicate_and_unknown_groups_are_rejected() -> None:
    _, engine, _ = engine_with_bus()
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    with pytest.raises(DuplicateOptionsGroupError):
        engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    with pytest.raises(UnknownOptionsGroupError):
        engine.state("missing")
    with pytest.raises(UnknownOptionsGroupError):
        engine.cancel("missing")


def test_engine_requires_a_mid_touch_model_and_valid_bus() -> None:
    bus = EventBus(SimClock(STAMP))
    with pytest.raises(TypeError):
        OptionsMatchingEngine(bus, cast(MidTouch, SimpleTouch()))
    with pytest.raises(TypeError):
        OptionsMatchingEngine(cast(EventBus, "not-a-bus"))


def test_engine_drives_the_multi_leg_oms_atomically() -> None:
    bus = EventBus(SimClock(STAMP))
    oms = MultiLegOMS()
    fills: list[Fill] = []
    engine = OptionsMatchingEngine(bus, oms=oms)
    bus.subscribe(Fill, fills.append)

    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    bus.drain()
    assert oms.state("vertical").status is GroupStatus.ACCEPTED

    for quote_event in complete_quotes():
        bus.publish(quote_event)
    bus.drain()

    snapshot = oms.state("vertical")
    assert snapshot.status is GroupStatus.FILLED
    assert engine.state("vertical").status is GroupStatus.FILLED
    assert oms.fills("vertical") == tuple(fills)
    long_leg, short_leg = snapshot.legs
    assert long_leg.average_fill_price == Decimal("4.00")
    assert long_leg.filled_quantity == Decimal("2")
    assert short_leg.average_fill_price == Decimal("2.00")
    assert snapshot.net_price == Decimal("2.00")
    assert snapshot.net_cash == Decimal("-4.00")

    engine.submit(vertical("vertical-2"), GroupOrderType.NET_DEBIT)
    bus.publish(quote(LONG_CALL, "long-2", 10, bid="3.95", ask="4.05"))
    bus.publish(quote(SHORT_CALL, "short-2", 11, bid="1.95", ask="2.05"))
    bus.drain()
    assert oms.state("vertical-2").status is GroupStatus.FILLED
    first_ids = {fill.fill_id for fill in oms.fills("vertical")}
    second_ids = {fill.fill_id for fill in oms.fills("vertical-2")}
    assert first_ids.isdisjoint(second_ids)


def test_blocked_group_leaves_engine_and_oms_accepted() -> None:
    bus = EventBus(SimClock(STAMP))
    oms = MultiLegOMS()
    fills: list[Fill] = []
    engine = OptionsMatchingEngine(bus, oms=oms)
    bus.subscribe(Fill, fills.append)
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    bus.publish(quote(LONG_CALL, "long", 1, bid="3.95", ask="4.05"))
    bus.publish(quote(SHORT_CALL, "short", 2, bid="1.95", ask="2.05", bid_quantity="1"))
    bus.drain()

    assert fills == []
    assert engine.state("vertical").status is GroupStatus.ACCEPTED
    assert oms.state("vertical").status is GroupStatus.ACCEPTED


def test_response_latency_uses_the_injected_clock() -> None:
    bus, engine, fills = engine_with_bus(latency=timedelta(microseconds=5))
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    bus.drain()
    for quote_event in complete_quotes():
        bus.publish(quote_event)
    bus.drain()

    assert all(fill.timestamp == STAMP + timedelta(microseconds=7) for fill in fills)


def test_off_tick_and_spread_gates_block_the_group_until_quoted_well() -> None:
    bus, engine, fills = engine_with_bus(model=MidTouch(maximum_spread=Decimal("0.10")))
    engine.submit(vertical(), GroupOrderType.NET_DEBIT)
    bus.publish(quote(LONG_CALL, "long", 1, bid="3.95", ask="4.05"))
    bus.publish(quote(SHORT_CALL, "wide", 2, bid="1.95", ask="2.20"))
    bus.drain()
    assert fills == []

    bus.publish(quote(SHORT_CALL, "off-tick", 3, bid="1.955", ask="2.055"))
    bus.drain()
    assert fills == []

    bus.publish(quote(SHORT_CALL, "good", 4, bid="1.95", ask="2.05"))
    bus.drain()
    assert len(fills) == 2


def test_deterministic_replay_produces_identical_fill_batches() -> None:
    def replay() -> tuple[list[Fill], str]:
        bus, engine, fills = engine_with_bus(venue="options")
        engine.submit(vertical(), GroupOrderType.NET_DEBIT)
        bus.drain()
        for quote_event in complete_quotes():
            bus.publish(quote_event)
        bus.drain()
        return fills, bus.dispatch_hash

    first_fills, first_hash = replay()
    second_fills, second_hash = replay()

    assert [(fill.fill_id, fill.timestamp, fill.sequence) for fill in first_fills] == [
        (fill.fill_id, fill.timestamp, fill.sequence) for fill in second_fills
    ]
    assert [
        (str(fill.symbol), fill.side, fill.quantity, fill.price) for fill in first_fills
    ] == [
        (str(fill.symbol), fill.side, fill.quantity, fill.price)
        for fill in second_fills
    ]
    assert first_hash == second_hash


def test_mid_touch_drops_into_the_single_leg_sim_venue() -> None:
    bus = EventBus(SimClock(STAMP))
    fills: list[Fill] = []
    bus.subscribe(Fill, fills.append)
    venue = SimVenue(bus, MidTouch())
    bus.publish(quote(LONG_CALL, "q", 0, bid="3.95", ask="4.05"))
    bus.drain()

    market_order = SingleLegOrder(
        "event-market",
        STAMP,
        0,
        "market",
        LONG_CALL,
        Side.BUY,
        Decimal("5"),
        OrderType.MARKET,
    )
    limit_order = SingleLegOrder(
        "event-limit",
        STAMP + timedelta(microseconds=1),
        1,
        "limit",
        LONG_CALL,
        Side.BUY,
        Decimal("5"),
        OrderType.LIMIT,
        limit_price=Decimal("4.05"),
    )
    venue.submit(market_order)
    venue.submit(limit_order)
    bus.drain()

    assert [fill.price for fill in fills] == [Decimal("4.05"), Decimal("4.00")]
    assert [fill.liquidity for fill in fills] == ["taker", "maker"]
