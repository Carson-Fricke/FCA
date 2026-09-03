from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from harness.core.symbols import Symbol
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import OrderType, Side, SingleLegOrder
from harness.fills.latency_size import (
    LatencySize,
    LatencySizeConfig,
    RemainderPolicy,
    load_latency_size,
    load_latency_size_config,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")


def quote(
    event_id: str,
    *,
    bid: str = "100",
    ask: str = "101",
    bid_quantity: str = "100",
    ask_quantity: str = "100",
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP,
        int(event_id.removeprefix("q-")),
        AAPL,
        Decimal(bid),
        Decimal(bid_quantity),
        Decimal(ask),
        Decimal(ask_quantity),
    )


def trade(event_id: str, price: str, quantity: str = "100") -> TradeEvent:
    return TradeEvent(
        event_id,
        STAMP,
        int(event_id.removeprefix("t-")),
        AAPL,
        Decimal(price),
        Decimal(quantity),
    )


def order(
    order_id: str,
    *,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit_price: str | None = None,
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
        limit_price=None if limit_price is None else Decimal(limit_price),
    )


@pytest.mark.parametrize(
    ("policy", "expected_second"),
    [(RemainderPolicy.CANCEL, None), (RemainderPolicy.REST, Decimal("100"))],
)
def test_300_share_order_uses_displayed_size_and_applies_remainder_policy(
    policy: RemainderPolicy, expected_second: Decimal | None
) -> None:
    model = LatencySize(remainder_policy=policy)
    buy = order(f"size-{policy.value}")

    first = model.decide(buy, quote("q-1"))
    assert first is not None
    assert first.quantity == Decimal("100")
    assert model.is_cancelled(buy.order_id) is (policy is RemainderPolicy.CANCEL)

    second = model.decide(buy, quote("q-2"), Decimal("200"))
    if expected_second is None:
        assert second is None
    else:
        assert second is not None
        assert second.quantity == expected_second


def test_partial_fills_across_quote_events_are_snapshot_deterministic() -> None:
    model = LatencySize()
    buy = order("snapshots")
    first_quote = quote("q-1", ask="101", ask_quantity="100")
    second_quote = quote("q-2", ask="100", ask_quantity="100")

    first = model.decide(buy, first_quote)
    second = model.decide(buy, second_quote, Decimal("200"))

    assert first is not None and second is not None
    assert (first.quantity, first.price, first.market) == (
        Decimal("100"),
        Decimal("101"),
        first_quote,
    )
    assert (second.quantity, second.price, second.market) == (
        Decimal("100"),
        Decimal("100"),
        second_quote,
    )
    assert model.decide(buy, first_quote, Decimal("100")) == first


def test_inside_spread_limit_requires_qualifying_print_by_default() -> None:
    model = LatencySize()
    buy = order("inside", order_type=OrderType.LIMIT, limit_price="100.50")

    assert model.decide(buy, quote("q-1", bid="100", ask="101")) is None
    assert model.decide(buy, trade("t-1", "100.75")) is None

    decision = model.decide(buy, trade("t-2", "100.50"))
    assert decision is not None
    assert decision.quantity == Decimal("100")
    assert decision.price == Decimal("100.50")
    assert decision.liquidity == "maker"


def test_configuration_changes_latency_and_inside_spread_outcome() -> None:
    pessimistic = LatencySize.from_config(
        LatencySizeConfig(
            response_latency=timedelta(microseconds=25),
            require_trade_through=True,
        )
    )
    optimistic = LatencySize(
        response_latency=timedelta(microseconds=50),
        require_trade_through=False,
    )
    buy = order("parameters", order_type=OrderType.LIMIT, limit_price="100.50")
    inside = quote("q-1", bid="100", ask="101")

    assert pessimistic.metadata.response_latency_us == 25
    assert pessimistic.decide(buy, inside) is None
    decision = optimistic.decide(buy, inside)
    assert decision is not None
    assert decision.price == Decimal("100.50")
    assert optimistic.metadata.response_latency_us == 50


def test_yaml_parameters_are_loaded_independently_and_change_outcomes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "pessimistic.yaml"
    second = tmp_path / "optimistic.yaml"
    first.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "model: latency_size",
                "response_latency_us: 25",
                "remainder_policy: cancel",
                "require_trade_through: true",
            )
        ),
        encoding="utf-8",
    )
    second.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "model: latency_size",
                "response_latency_us: 50",
                "remainder_policy: rest",
                "require_trade_through: false",
            )
        ),
        encoding="utf-8",
    )

    pessimistic = load_latency_size(first)
    optimistic = load_latency_size(second)
    buy = order("yaml", order_type=OrderType.LIMIT, limit_price="100.50")
    inside = quote("q-1", bid="100", ask="101")

    assert pessimistic.metadata != optimistic.metadata
    assert pessimistic.decide(buy, inside) is None
    assert optimistic.decide(buy, inside) is not None


def test_latency_metadata_rejects_invalid_values(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="timedelta"):
        LatencySize(response_latency=cast(timedelta, 0.1))
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("schema_version: 1\nunknown: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        load_latency_size_config(malformed)
