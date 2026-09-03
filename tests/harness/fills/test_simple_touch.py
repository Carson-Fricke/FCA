from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest

from harness.core.symbols import Symbol
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import OrderType, Side, SingleLegOrder
from harness.fills.simple_touch import FillDecision, SimpleTouch

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")


def quote(
    event_id: str,
    *,
    bid: str = "100",
    ask: str = "101",
    bid_quantity: str = "10",
    ask_quantity: str = "10",
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP,
        0,
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
    limit_price: str | None = None,
    stop_price: str | None = None,
) -> SingleLegOrder:
    return SingleLegOrder(
        f"event-{order_id}",
        STAMP,
        0,
        order_id,
        AAPL,
        side,
        Decimal(quantity),
        order_type,
        limit_price=None if limit_price is None else Decimal(limit_price),
        stop_price=None if stop_price is None else Decimal(stop_price),
    )


@pytest.mark.parametrize(
    ("side", "expected_price"),
    [(Side.BUY, Decimal("101")), (Side.SELL, Decimal("100"))],
)
def test_market_orders_touch_the_correct_side(
    side: Side, expected_price: Decimal
) -> None:
    decision = SimpleTouch().decide(
        order(f"market-{side.value}", side, OrderType.MARKET), quote("q")
    )

    assert isinstance(decision, FillDecision)
    assert decision.quantity == Decimal("10")
    assert decision.price == expected_price
    assert decision.decision_price == expected_price
    assert decision.triggering_market.event_id == "q"


@pytest.mark.parametrize(
    ("side", "limit", "quote_values", "expected"),
    [
        (Side.BUY, "101", {"ask": "101"}, Decimal("101")),
        (Side.SELL, "100", {"bid": "100"}, Decimal("100")),
    ],
)
def test_limits_cross_at_touch_and_rest_when_not_crossing(
    side: Side,
    limit: str,
    quote_values: dict[str, str],
    expected: Decimal,
) -> None:
    crossing = SimpleTouch().decide(
        order(f"cross-{side.value}", side, OrderType.LIMIT, limit_price=limit),
        quote("crossing", **quote_values),
    )
    resting = SimpleTouch().decide(
        order(f"rest-{side.value}", side, OrderType.LIMIT, limit_price=limit),
        quote("resting", bid="99", ask="102"),
    )

    assert crossing is not None
    assert crossing.price == expected
    assert resting is None


def test_partial_quantity_is_bounded_by_displayed_touch_quantity() -> None:
    decision = SimpleTouch().decide(
        order("partial", Side.BUY, OrderType.MARKET, quantity="10"),
        quote("small-ask", ask_quantity="3"),
    )

    assert decision is not None
    assert decision.quantity == Decimal("3")


@pytest.mark.parametrize(
    ("side", "stop", "triggered", "not_triggered"),
    [
        (
            Side.BUY,
            "101",
            quote("buy-trigger", ask="101"),
            quote("buy-rest", ask="100"),
        ),
        (
            Side.SELL,
            "100",
            quote("sell-trigger", bid="100"),
            quote("sell-rest", bid="101"),
        ),
    ],
)
def test_stop_orders_arm_only_on_the_correct_touch(
    side: Side, stop: str, triggered: QuoteEvent, not_triggered: QuoteEvent
) -> None:
    stop_order = order(f"stop-{side.value}", side, OrderType.STOP, stop_price=stop)
    plugin = SimpleTouch()

    assert plugin.is_triggered(stop_order, triggered)
    assert not plugin.is_triggered(stop_order, not_triggered)
    assert plugin.decide(stop_order, not_triggered) is None
    assert plugin.decide(stop_order, triggered) is not None


def test_stop_limit_requires_both_trigger_and_limit_touch() -> None:
    stop_order = order(
        "stop-limit",
        Side.BUY,
        OrderType.STOP_LIMIT,
        limit_price="102",
        stop_price="101",
    )
    plugin = SimpleTouch()

    decision = plugin.decide(stop_order, quote("trigger", ask="101"))
    assert decision is not None
    assert decision.triggering_market.event_id == "trigger"

    no_limit_touch = order(
        "stop-limit-rest",
        Side.BUY,
        OrderType.STOP_LIMIT,
        limit_price="100.50",
        stop_price="101",
    )
    assert plugin.decide(no_limit_touch, quote("armed", ask="101")) is None


def test_trade_snapshots_are_supported_without_float_conversion() -> None:
    traded = TradeEvent("trade", STAMP, 2, AAPL, Decimal("100.25"), Decimal("2"))
    decision = SimpleTouch().decide(
        order("trade-market", Side.SELL, OrderType.MARKET, quantity="3"), traded
    )

    assert decision is not None
    assert decision.quantity == Decimal("2")
    assert decision.market is traded
    with pytest.raises(TypeError):
        SimpleTouch().decide(
            order("float", Side.BUY, OrderType.MARKET),
            quote("q"),
            cast(Decimal, 0.5),
        )


def test_trade_does_not_qualify_a_resting_limit_touch() -> None:
    traded = TradeEvent("trade-limit", STAMP, 2, AAPL, Decimal("99"), Decimal("10"))

    assert (
        SimpleTouch().decide(
            order(
                "limit-trade",
                Side.BUY,
                OrderType.LIMIT,
                quantity="2",
                limit_price="100",
            ),
            traded,
        )
        is None
    )
