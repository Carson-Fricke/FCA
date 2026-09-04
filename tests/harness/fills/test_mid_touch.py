from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from harness.core.symbols import Symbol
from harness.core.types.common import DecimalInput
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import OrderType, Side, SingleLegOrder
from harness.fills.mid_touch import MidTouch, MidTouchConfig, load_mid_touch
from harness.fills.simple_touch import MarketSnapshot

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
OPTION = Symbol("AAPL240119C00150000")


def quote(
    event_id: str = "q",
    *,
    bid: str = "1.00",
    ask: str = "1.02",
    bid_quantity: str = "10",
    ask_quantity: str = "10",
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP,
        0,
        OPTION,
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
        OPTION,
        side,
        Decimal(quantity),
        order_type,
        limit_price=None if limit_price is None else Decimal(limit_price),
        stop_price=None if stop_price is None else Decimal(stop_price),
    )


@pytest.mark.parametrize(
    ("side", "touch_price"),
    [(Side.BUY, Decimal("1.02")), (Side.SELL, Decimal("1.00"))],
)
def test_market_orders_take_the_far_touch(side: Side, touch_price: Decimal) -> None:
    decision = MidTouch().decide(order("market", side, OrderType.MARKET), quote())

    assert decision is not None
    assert decision.quantity == Decimal("10")
    assert decision.price == touch_price
    assert decision.decision_price == touch_price
    assert decision.liquidity == "taker"


@pytest.mark.parametrize(
    ("side", "limit", "midpoint"),
    [(Side.BUY, "1.02", Decimal("1.01")), (Side.SELL, "1.00", Decimal("1.01"))],
)
def test_limit_orders_fill_at_the_exact_midpoint_after_an_adverse_touch(
    side: Side, limit: str, midpoint: Decimal
) -> None:
    decision = MidTouch().decide(
        order("limit", side, OrderType.LIMIT, limit_price=limit), quote()
    )

    assert decision is not None
    assert decision.price == midpoint
    assert decision.decision_price == midpoint
    assert decision.liquidity == "maker"
    assert decision.triggering_market.event_id == "q"


@pytest.mark.parametrize(
    ("side", "limit"),
    [(Side.BUY, "1.01"), (Side.SELL, "1.01")],
)
def test_resting_limits_never_fill_without_the_adverse_touch(
    side: Side, limit: str
) -> None:
    assert (
        MidTouch().decide(
            order("resting", side, OrderType.LIMIT, limit_price=limit), quote()
        )
        is None
    )


@pytest.mark.parametrize(
    ("side", "limit", "fillable", "blocked"),
    [
        (Side.BUY, "1.01", "1.01", "0.99"),
        (Side.SELL, "1.01", "1.01", "1.03"),
    ],
)
def test_optimistic_mid_cross_trigger_is_not_the_default(
    side: Side, limit: str, fillable: str, blocked: str
) -> None:
    model = MidTouch(require_adverse_touch=False)
    fillable_order = order("mid-cross", side, OrderType.LIMIT, limit_price=fillable)
    blocked_order = order("above-mid", side, OrderType.LIMIT, limit_price=blocked)

    assert MidTouch().decide(fillable_order, quote()) is None
    decision = model.decide(fillable_order, quote())
    assert decision is not None
    assert decision.price == Decimal("1.01")
    assert decision.liquidity == "maker"
    assert model.decide(blocked_order, quote()) is None


@pytest.mark.parametrize(
    ("tick_size", "bid", "ask", "fillable"),
    [
        ("0.01", "1.00", "1.02", True),
        ("0.01", "1.00", "1.03", False),
        ("0.05", "1.00", "1.03", False),
        ("0.05", "1.00", "1.10", True),
        ("0.005", "0.10", "0.15", True),
    ],
)
def test_midpoints_must_be_exact_tick_multiples(
    tick_size: str, bid: str, ask: str, fillable: bool
) -> None:
    model = MidTouch(tick_size=Decimal(tick_size))
    deep_limit = order("tick", Side.BUY, OrderType.LIMIT, limit_price="99")

    decision = model.decide(deep_limit, quote(bid=bid, ask=ask))

    assert (decision is not None) is fillable
    if decision is not None:
        midpoint = (Decimal(bid) + Decimal(ask)) / Decimal("2")
        assert decision.price == midpoint
        assert model.mid_execution(quote(bid=bid, ask=ask)) == midpoint
    else:
        assert model.mid_execution(quote(bid=bid, ask=ask)) is None


@pytest.mark.parametrize(
    ("maximum_spread", "bid", "ask", "fillable"),
    [
        ("0.02", "1.00", "1.02", True),
        ("0.02", "0.99", "1.03", False),
        ("0", "1.00", "1.00", True),
        ("0", "1.00", "1.01", False),
    ],
)
def test_maximum_spread_gates_midpoint_fills(
    maximum_spread: str, bid: str, ask: str, fillable: bool
) -> None:
    model = MidTouch(maximum_spread=Decimal(maximum_spread))

    decision = model.decide(
        order("spread", Side.BUY, OrderType.LIMIT, limit_price="99"),
        quote(bid=bid, ask=ask),
    )

    assert (decision is not None) is fillable


def test_default_model_has_no_spread_gate() -> None:
    decision = MidTouch().decide(
        order("wide", Side.BUY, OrderType.LIMIT, limit_price="99"),
        quote(bid="1.00", ask="3.00"),
    )

    assert decision is not None
    assert decision.price == Decimal("2.00")


@pytest.mark.parametrize(
    ("side", "size_field"),
    [(Side.BUY, "ask_quantity"), (Side.SELL, "bid_quantity")],
)
def test_displayed_size_caps_mid_and_touch_fills(side: Side, size_field: str) -> None:
    market_order = order("size", side, OrderType.MARKET)
    limit_order_ = order(
        "size-limit",
        side,
        OrderType.LIMIT,
        limit_price="1.02" if side is Side.BUY else "1.00",
    )
    small_quote = quote("small", **{size_field: "3"})

    market_decision = MidTouch().decide(market_order, small_quote)
    limit_decision = MidTouch().decide(limit_order_, small_quote)

    assert market_decision is not None
    assert limit_decision is not None
    assert market_decision.quantity == Decimal("3")
    assert limit_decision.quantity == Decimal("3")


def test_available_quantity_overrides_displayed_size() -> None:
    model = MidTouch()
    limit = order("override", Side.BUY, OrderType.LIMIT, limit_price="1.02")

    assert model.decide(limit, quote(), available_quantity=Decimal("0")) is None
    override = model.decide(limit, quote(), available_quantity=Decimal("5"))
    assert override is not None
    assert override.quantity == Decimal("5")
    larger = model.decide(limit, quote(), available_quantity=Decimal("20"))
    assert larger is not None
    assert larger.quantity == Decimal("10")


def test_trade_prints_fill_takers_and_never_resting_limits() -> None:
    traded = TradeEvent("trade", STAMP, 1, OPTION, Decimal("1.01"), Decimal("7"))
    model = MidTouch()

    market_decision = model.decide(order("market", Side.BUY, OrderType.MARKET), traded)
    assert market_decision is not None
    assert market_decision.quantity == Decimal("7")
    assert market_decision.price == Decimal("1.01")
    assert market_decision.liquidity == "taker"

    assert (
        model.decide(
            order("limit", Side.BUY, OrderType.LIMIT, limit_price="1.02"), traded
        )
        is None
    )


@pytest.mark.parametrize(
    ("side", "stop", "triggered", "not_triggered"),
    [
        (Side.BUY, "1.00", quote("buy-arm", ask="1.00"), quote("buy-idle", ask="0.99")),
        (
            Side.SELL,
            "1.00",
            quote("sell-arm", bid="1.00"),
            quote("sell-idle", bid="1.01"),
        ),
    ],
)
def test_stop_orders_arm_only_on_the_adverse_touch(
    side: Side, stop: str, triggered: QuoteEvent, not_triggered: QuoteEvent
) -> None:
    stop_order = order("stop", side, OrderType.STOP, stop_price=stop)
    model = MidTouch()

    assert model.is_triggered(stop_order, triggered)
    assert not model.is_triggered(stop_order, not_triggered)
    assert model.decide(stop_order, not_triggered) is None
    decision = model.decide(stop_order, triggered)
    assert decision is not None
    assert decision.liquidity == "taker"


def test_stop_limit_requires_trigger_and_marketable_limit() -> None:
    model = MidTouch()
    stop_limit = order(
        "stop-limit",
        Side.BUY,
        OrderType.STOP_LIMIT,
        limit_price="1.02",
        stop_price="1.00",
    )

    armed = model.decide(stop_limit, quote("armed", bid="1.00", ask="1.02"))
    assert armed is not None
    assert armed.price == Decimal("1.01")
    assert armed.liquidity == "maker"

    beyond_limit_order = order(
        "stop-limit-tight",
        Side.BUY,
        OrderType.STOP_LIMIT,
        limit_price="1.01",
        stop_price="1.00",
    )
    beyond_limit = model.decide(
        beyond_limit_order, quote("beyond", bid="1.00", ask="1.03")
    )
    assert beyond_limit is None


def test_stop_triggered_passthrough_fills_like_a_market_order() -> None:
    decision = MidTouch().decide(
        order("stop", Side.BUY, OrderType.STOP, stop_price="1.00"),
        quote("below", bid="0.98", ask="0.99"),
        stop_triggered=True,
    )

    assert decision is not None
    assert decision.price == Decimal("0.99")
    assert decision.liquidity == "taker"


def test_default_fills_never_beat_the_documented_adverse_midpoint_rule() -> None:
    model = MidTouch()
    marketable_buy = order("ok", Side.BUY, OrderType.LIMIT, limit_price="1.05")

    decision = model.decide(marketable_buy, quote())
    assert decision is not None
    exact_midpoint = (Decimal("1.00") + Decimal("1.02")) / Decimal("2")
    assert decision.price == exact_midpoint
    assert decision.price <= Decimal("1.05")
    assert decision.price >= Decimal("1.00")


def test_exact_decimal_math_without_floats() -> None:
    model = MidTouch(tick_size=Decimal("0.005"))
    fine_quote = quote("fine", bid="0.10", ask="0.15")
    decision = model.decide(
        order("fine", Side.BUY, OrderType.LIMIT, limit_price="1"), fine_quote
    )

    assert decision is not None
    assert decision.price == Decimal("0.125")
    assert decision.price.as_tuple().exponent == -3
    with pytest.raises(TypeError):
        model.decide(
            order("float", Side.BUY, OrderType.MARKET),
            quote(),
            cast(Decimal, 0.5),
        )
    with pytest.raises(ValueError):
        model.decide(order("zero", Side.BUY, OrderType.MARKET), quote(), Decimal("0"))


def test_validation_rejects_mismatched_symbols_and_types() -> None:
    model = MidTouch()
    with pytest.raises(ValueError):
        model.decide(
            SingleLegOrder(
                "event-x",
                STAMP,
                0,
                "other",
                Symbol("MSFT"),
                Side.BUY,
                Decimal("1"),
                OrderType.MARKET,
            ),
            quote(),
        )
    with pytest.raises(TypeError):
        model.decide(
            order("bad-market", Side.BUY, OrderType.MARKET),
            cast(MarketSnapshot, "not-an-event"),
        )
    with pytest.raises(TypeError):
        model.decide(cast(SingleLegOrder, "not-an-order"), quote())


def test_config_metadata_and_validation() -> None:
    config = MidTouchConfig(
        tick_size="0.05",
        maximum_spread="0.50",
        require_adverse_touch=False,
        response_latency=timedelta(microseconds=10),
    )
    model = MidTouch.from_config(config)

    assert model.tick_size == Decimal("0.05")
    assert model.maximum_spread == Decimal("0.50")
    assert model.require_adverse_touch is False
    assert model.latency == timedelta(microseconds=10)
    metadata = model.metadata
    assert metadata.displayed_size_constrained is True
    assert metadata.require_adverse_touch is False
    assert metadata.tick_size == Decimal("0.05")
    assert metadata.maximum_spread == Decimal("0.50")
    assert metadata.response_latency_us == 10

    with pytest.raises(ValueError):
        MidTouchConfig(tick_size=Decimal("0"))
    with pytest.raises(ValueError):
        MidTouchConfig(maximum_spread=Decimal("-0.01"))
    with pytest.raises(ValueError):
        MidTouchConfig(response_latency=timedelta(microseconds=-1))
    with pytest.raises(TypeError):
        MidTouchConfig(require_adverse_touch=cast(bool, "yes"))
    with pytest.raises(TypeError):
        MidTouchConfig(tick_size=cast(DecimalInput, 0.5))


def test_yaml_loader_roundtrip(tmp_path: Path) -> None:
    document = tmp_path / "mid_touch.yaml"
    document.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "model: mid_touch",
                "response_latency: 10us",
                "tick_size: 0.05",
                "maximum_spread: 0.30",
                "require_adverse_touch: true",
            )
        ),
        encoding="utf-8",
    )
    model = load_mid_touch(document)

    assert model.tick_size == Decimal("0.05")
    assert model.maximum_spread == Decimal("0.30")
    assert model.require_adverse_touch is True
    assert model.latency == timedelta(microseconds=10)

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text("model: mid_touch\nunknown: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_mid_touch(unknown)
    wrong_model = tmp_path / "wrong.yaml"
    wrong_model.write_text("model: trade_through\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_mid_touch(wrong_model)
    wrong_version = tmp_path / "version.yaml"
    wrong_version.write_text("schema_version: 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_mid_touch(wrong_version)


def test_repeated_decisions_are_deterministic() -> None:
    model = MidTouch()
    limit_order_ = order("same", Side.BUY, OrderType.LIMIT, limit_price="1.02")

    first = model.decide(limit_order_, quote("one"))
    second = model.decide(limit_order_, quote("one"))

    assert first == second
    assert first is not None
    assert second is not None
    assert first.price == second.price == Decimal("1.01")
