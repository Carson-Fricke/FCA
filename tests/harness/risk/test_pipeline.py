from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter_ns
from typing import cast

import pytest

from harness.configuration import BacktestConfig, load_example
from harness.core.symbols import Symbol
from harness.core.types.orders import OrderRequest, Side, SingleLegOrder
from harness.portfolio.hub import AggregatePosition, PortfolioSnapshot
from harness.risk.limits import (
    DailyLossHalt,
    MaxGrossExposure,
    MaxNetExposure,
    MaxOrderCount,
    MaxOrderNotional,
    MaxOrderSize,
    MaxPositionNotional,
)
from harness.risk.models import RiskAction, RiskContext, RiskRule, RuleDecision
from harness.risk.pipeline import RiskPipeline

STAMP = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
ZERO = Decimal("0")


def order(
    quantity: str,
    *,
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    order_id: str = "order-1",
) -> SingleLegOrder:
    return SingleLegOrder(
        event_id=f"event-{order_id}",
        timestamp=STAMP,
        sequence=1,
        order_id=order_id,
        symbol=Symbol(symbol),
        side=side,
        quantity=Decimal(quantity),
    )


def position(
    symbol: str,
    quantity: str,
    price: str,
) -> AggregatePosition:
    signed_quantity = Decimal(quantity)
    mark = Decimal(price)
    return AggregatePosition(
        symbol=Symbol(symbol),
        quantity=signed_quantity,
        long_quantity=max(signed_quantity, ZERO),
        short_quantity=max(-signed_quantity, ZERO),
        cost_basis=signed_quantity * mark,
        cash_flow=-signed_quantity * mark,
        average_cost=mark,
        realized_pnl=ZERO,
        mark_price=mark,
        market_value=signed_quantity * mark,
        unrealized_pnl=ZERO,
    )


def snapshot(*positions: AggregatePosition) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_id="risk-account",
        initial_cash=Decimal("100000"),
        cash=Decimal("100000"),
        positions=positions,
        books=(),
        fill_count=0,
    )


def context(
    *,
    positions: tuple[AggregatePosition, ...] = (),
    prices: dict[str, str] | None = None,
    daily_loss: str = "0",
    order_count: int = 0,
    strategy_id: str | None = None,
) -> RiskContext:
    return RiskContext(
        portfolio=snapshot(*positions),
        prices={}
        if prices is None
        else {key: Decimal(value) for key, value in prices.items()},
        daily_loss=Decimal(daily_loss),
        order_count=order_count,
        strategy_id=strategy_id,
    )


def pipeline(rule: RiskRule) -> RiskPipeline:
    return RiskPipeline((rule,))


def risk_rules(*rules: RiskRule) -> tuple[RiskRule, ...]:
    return rules


@pytest.mark.parametrize(
    ("rule", "candidate", "expected"),
    [
        (MaxOrderNotional({"limit": Decimal("1000")}), order("10"), RiskAction.ALLOW),
        (
            MaxOrderNotional({"limit": Decimal("1000")}),
            order("10.01"),
            RiskAction.REDUCE,
        ),
        (MaxOrderNotional({"limit": Decimal("0")}), order("1"), RiskAction.REJECT),
        (
            MaxPositionNotional({"limit": Decimal("1000")}),
            order("5"),
            RiskAction.ALLOW,
        ),
        (
            MaxPositionNotional({"limit": Decimal("1000")}),
            order("10.01"),
            RiskAction.REDUCE,
        ),
        (
            MaxPositionNotional({"limit": Decimal("0")}),
            order("1"),
            RiskAction.REJECT,
        ),
        (MaxGrossExposure({"limit": Decimal("1000")}), order("10"), RiskAction.ALLOW),
        (
            MaxGrossExposure({"limit": Decimal("1000")}),
            order("10.01"),
            RiskAction.REDUCE,
        ),
        (MaxGrossExposure({"limit": Decimal("0")}), order("1"), RiskAction.REJECT),
        (MaxNetExposure({"limit": Decimal("1000")}), order("10"), RiskAction.ALLOW),
        (MaxNetExposure({"limit": Decimal("1000")}), order("10.01"), RiskAction.REDUCE),
        (MaxNetExposure({"limit": Decimal("0")}), order("1"), RiskAction.REJECT),
        (MaxOrderSize({"limit": Decimal("10")}), order("10"), RiskAction.ALLOW),
        (MaxOrderSize({"limit": Decimal("10")}), order("10.01"), RiskAction.REDUCE),
        (MaxOrderSize({"limit": Decimal("0")}), order("1"), RiskAction.REJECT),
    ],
)
def test_notional_and_size_rules_have_exact_boundaries(
    rule: RiskRule, candidate: SingleLegOrder, expected: RiskAction
) -> None:
    result = rule.evaluate(candidate, context(prices={"AAPL": "100"}))
    assert result.action is expected


@pytest.mark.parametrize(
    ("rule", "risk_context", "expected"),
    [
        (
            MaxOrderCount({"limit": Decimal("2")}),
            context(order_count=1),
            RiskAction.ALLOW,
        ),
        (
            MaxOrderCount({"limit": Decimal("2")}),
            context(order_count=2),
            RiskAction.REJECT,
        ),
        (
            DailyLossHalt({"limit": Decimal("100")}),
            context(daily_loss="99.99"),
            RiskAction.ALLOW,
        ),
        (
            DailyLossHalt({"limit": Decimal("100")}),
            context(daily_loss="100"),
            RiskAction.REJECT,
        ),
    ],
)
def test_count_and_daily_loss_rules_use_inclusive_halt_boundaries(
    rule: RiskRule, risk_context: RiskContext, expected: RiskAction
) -> None:
    result = rule.evaluate(order("1"), risk_context)
    assert result.action is expected
    if isinstance(rule, DailyLossHalt) and expected is RiskAction.REJECT:
        assert result.halt is True


def test_ordered_gross_and_net_exposure_use_signed_position_values() -> None:
    risk_context = context(
        positions=(position("AAPL", "5", "100"), position("MSFT", "-5", "100")),
        prices={"AAPL": "100", "MSFT": "100"},
    )
    candidate = order("1", symbol="AAPL")

    gross = MaxGrossExposure({"limit": Decimal("1099")}).evaluate(
        candidate, risk_context
    )
    net = MaxNetExposure({"limit": Decimal("100")}).evaluate(candidate, risk_context)

    assert gross.action is RiskAction.REDUCE
    assert gross.metrics["gross_exposure"] == Decimal("1100")
    assert net.action is RiskAction.ALLOW
    assert net.metrics["net_exposure"] == Decimal("100")


def test_gross_exposure_preserves_side_when_a_order_closes_a_short() -> None:
    risk_context = context(positions=(position("AAPL", "-5", "100"),))

    result = MaxGrossExposure({"limit": Decimal("500")}).evaluate(
        order("3", side=Side.BUY), risk_context
    )

    assert result.action is RiskAction.ALLOW
    assert result.metrics["gross_exposure"] == Decimal("200")


def test_strategy_override_is_selected_without_mutating_base_parameters() -> None:
    rule = MaxOrderSize(
        {
            "limit": Decimal("10"),
            "overrides": {"small-strategy": {"limit": Decimal("2")}},
        }
    )
    candidate = order("3")

    base = rule.evaluate(candidate, context(strategy_id="other"))
    override = rule.evaluate(candidate, context(strategy_id="small-strategy"))

    assert base.action is RiskAction.ALLOW
    assert override.action is RiskAction.REDUCE
    assert isinstance(override.order, SingleLegOrder)
    assert override.order.quantity == Decimal("2")
    assert rule.parameters_for(context(strategy_id="other"))["limit"] == Decimal("10")


def test_reduced_orders_restart_from_first_rule_and_record_recheck_veto() -> None:
    class VetoReducedOrder:
        rule_id = "veto-reduced"

        def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
            del context
            if isinstance(order, SingleLegOrder) and order.quantity < Decimal("10"):
                return RuleDecision.reject(order, "reduced order veto")
            return RuleDecision.allow(order)

    result = RiskPipeline(
        risk_rules(
            MaxOrderSize({"limit": Decimal("5")}, rule_id="size"),
            VetoReducedOrder(),
        )
    ).evaluate(order("10"), context())

    assert result.action is RiskAction.REJECT
    assert isinstance(result.order, SingleLegOrder)
    assert result.order.quantity == Decimal("5")
    assert result.reason == "reduced order veto"
    assert result.veto_reasons == ("max_order_size reduced order", "reduced order veto")
    assert [audit.rule_id for audit in result.audit_trail] == [
        "size",
        "size",
        "veto-reduced",
    ]
    assert result.audit_trail[1].recheck is True
    assert result.audit_trail[2].recheck is True


def test_pipeline_preserves_order_and_rechecks_reduced_order() -> None:
    result = RiskPipeline(
        risk_rules(
            MaxOrderNotional({"limit": Decimal("500")}, rule_id="notional"),
            MaxOrderSize({"limit": Decimal("4")}, rule_id="size"),
        )
    ).evaluate(order("10"), context(prices={"AAPL": "100"}))

    assert result.action is RiskAction.REDUCE
    assert isinstance(result.order, SingleLegOrder)
    assert result.order.quantity == Decimal("4")
    assert [audit.rule_id for audit in result.audit_trail] == [
        "notional",
        "notional",
        "size",
        "notional",
        "size",
    ]
    assert all(audit.recheck for audit in result.audit_trail[1:])


def test_yaml_configuration_builds_existing_example_rules() -> None:
    configuration = cast(BacktestConfig, load_example("backtest"))
    result = RiskPipeline.from_config(configuration.risk_pipeline).evaluate(
        order("101"),
        context(prices={"AAPL": "100"}),
    )

    assert result.action is RiskAction.REDUCE
    assert isinstance(result.order, SingleLegOrder)
    assert result.order.quantity == Decimal("100")
    assert result.audit_trail[0].rule_id == "max-order-notional"


def test_twenty_position_evaluation_is_benchmark_shaped_without_flaky_timing() -> None:
    positions = tuple(position(f"SYM{index:02d}", "1", "100") for index in range(20))
    prices = {f"SYM{index:02d}": "100" for index in range(20)}
    risk_context = context(positions=positions, prices=prices)
    engine = RiskPipeline(risk_rules(MaxGrossExposure({"limit": Decimal("5000")})))
    candidate = order("1", symbol="SYM19")

    for _ in range(3):
        engine.evaluate(candidate, risk_context)
    samples = []
    for _ in range(10):
        start = perf_counter_ns()
        result = engine.evaluate(candidate, risk_context)
        samples.append(perf_counter_ns() - start)

    assert result.action is RiskAction.ALLOW
    # Use the best warmed sample to avoid turning scheduler noise into a failure.
    assert min(samples) < 1_000_000
