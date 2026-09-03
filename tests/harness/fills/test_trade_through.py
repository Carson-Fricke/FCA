from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from harness.core.symbols import Symbol
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import OrderType, Side, SingleLegOrder
from harness.fills.latency_size import RemainderPolicy
from harness.fills.trade_through import (
    TradeThrough,
    TradeThroughConfig,
    load_trade_through,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")


def quote(event_id: str = "quote") -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP,
        0,
        AAPL,
        Decimal("100"),
        Decimal("100"),
        Decimal("101"),
        Decimal("100"),
    )


def trade(price: str, quantity: str = "100", event_id: str = "trade") -> TradeEvent:
    return TradeEvent(
        event_id,
        STAMP,
        0,
        AAPL,
        Decimal(price),
        Decimal(quantity),
    )


def order(
    order_id: str,
    *,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.LIMIT,
    limit_price: str = "100.50",
    quantity: str = "300",
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
        limit_price=Decimal(limit_price)
        if order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
        else None,
        stop_price=Decimal("100")
        if order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
        else None,
    )


def test_strict_trade_through_requires_a_print_beyond_the_limit() -> None:
    model = TradeThrough()
    buy = order("strict")

    assert model.decide(buy, quote()) is None
    assert model.decide(buy, trade("100.50")) is None

    decision = model.decide(buy, trade("100.49"))
    assert decision is not None
    assert decision.quantity == Decimal("100")
    assert decision.price == Decimal("100.49")
    assert decision.liquidity == "maker"


@pytest.mark.parametrize(
    ("side", "limit", "at_limit", "through"),
    [
        (Side.BUY, "100.50", "100.50", "100.49"),
        (Side.SELL, "100.50", "100.50", "100.51"),
    ],
)
def test_trade_print_qualification_is_directional(
    side: Side, limit: str, at_limit: str, through: str
) -> None:
    model = TradeThrough()
    limit_order = order(f"direction-{side.value}", side=side, limit_price=limit)

    assert model.decide(limit_order, trade(at_limit)) is None
    decision = model.decide(limit_order, trade(through))
    assert decision is not None
    assert decision.price == Decimal(through)


def test_300_share_order_respects_trade_print_size_and_remainder_policy() -> None:
    cancel = TradeThrough(remainder_policy=RemainderPolicy.CANCEL)
    rest = TradeThrough(remainder_policy=RemainderPolicy.REST)
    cancel_order = order("cancel", quantity="300")
    rest_order = order("rest", quantity="300")
    print_event = trade("100.49", quantity="100")

    cancel_decision = cancel.decide(cancel_order, print_event)
    rest_decision = rest.decide(rest_order, print_event)
    assert cancel_decision is not None and rest_decision is not None
    assert cancel_decision.quantity == rest_decision.quantity == Decimal("100")
    assert cancel.is_cancelled(cancel_order.order_id)
    assert not rest.is_cancelled(rest_order.order_id)
    assert (
        rest.decide(rest_order, trade("100.48", event_id="trade-2"), Decimal("200"))
        is not None
    )


def test_parameters_change_threshold_and_metadata_without_global_coupling() -> None:
    default = TradeThrough()
    configured = TradeThrough.from_config(
        TradeThroughConfig(
            response_latency=timedelta(microseconds=40),
            minimum_trade_through=Decimal("0.10"),
            strict=False,
        )
    )
    buy = order("parameters")

    assert default.decide(buy, trade("100.50")) is None
    configured_decision = configured.decide(buy, trade("100.40"))
    assert configured_decision is not None
    assert configured_decision.price == Decimal("100.40")
    assert configured.metadata.response_latency_us == 40
    assert configured.metadata.minimum_trade_through == Decimal("0.10")


def test_yaml_parameters_produce_distinct_trade_through_results(tmp_path: Path) -> None:
    strict_path = tmp_path / "strict.yaml"
    permissive_path = tmp_path / "permissive.yaml"
    strict_path.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "model: trade_through",
                "response_latency_us: 10",
                "minimum_trade_through: 0.10",
                "strict: true",
            )
        ),
        encoding="utf-8",
    )
    permissive_path.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "model: trade_through",
                "response_latency_us: 20",
                "minimum_trade_through: 0",
                "strict: false",
            )
        ),
        encoding="utf-8",
    )

    strict = load_trade_through(strict_path)
    permissive = load_trade_through(permissive_path)
    buy = order("yaml")
    at_limit = trade("100.50")

    assert strict.metadata != permissive.metadata
    assert strict.decide(buy, at_limit) is None
    assert permissive.decide(buy, at_limit) is not None


def test_trade_through_snapshots_are_immutable_and_repeatable() -> None:
    model = TradeThrough()
    buy = order("snapshot", quantity="50")
    print_event = trade("100.49", quantity="50")

    first = model.decide(buy, print_event)
    second = TradeThrough().decide(buy, print_event)

    assert first == second
    assert first is not None
    assert first.triggering_market is print_event
    assert first.market_snapshot is print_event


def test_trade_through_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="minimum_trade_through"):
        TradeThrough(minimum_trade_through=Decimal("-0.01"))
