"""Focused tests for exact-decimal option collateral risk rules."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types.orders import MultiLegOrder, OrderLeg, Side, SingleLegOrder
from harness.portfolio.hub import AggregatePosition, PortfolioSnapshot
from harness.risk.collateral import (
    ContractCollateral,
    NoNakedShortOptions,
    OptionCollateralLimit,
    option_collateral_report,
)
from harness.risk.models import RiskAction, RiskContext, RiskRule
from harness.risk.pipeline import RiskPipeline

STAMP = datetime(2024, 11, 18, 14, 30, tzinfo=UTC)
ZERO = Decimal("0")
EXPIRY = date(2024, 11, 19)


def osi(
    root: str = "SPY",
    expiry: str = "241119",
    right: str = "C",
    strike: str = "500",
) -> str:
    units = int((Decimal(strike) * 1000).to_integral_value())
    return f"{root}{expiry}{right}{units:08d}"


def single(
    symbol: str,
    side: Side,
    quantity: str,
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


def multi(*legs: tuple[str, Side, str], order_id: str = "order-1") -> MultiLegOrder:
    return MultiLegOrder(
        event_id=f"event-{order_id}",
        timestamp=STAMP,
        sequence=1,
        order_id=order_id,
        legs=tuple(
            OrderLeg(Symbol(symbol), side, Decimal(quantity))
            for symbol, side, quantity in legs
        ),
    )


def position(symbol: str, quantity: str, price: str) -> AggregatePosition:
    signed = Decimal(quantity)
    mark = Decimal(price)
    return AggregatePosition(
        symbol=Symbol(symbol),
        quantity=signed,
        long_quantity=max(signed, ZERO),
        short_quantity=max(-signed, ZERO),
        cost_basis=signed * mark,
        cash_flow=-signed * mark,
        average_cost=mark,
        realized_pnl=ZERO,
        mark_price=mark,
        market_value=signed * mark,
        unrealized_pnl=ZERO,
    )


def context(
    *positions: AggregatePosition, strategy_id: str | None = None
) -> RiskContext:
    return RiskContext(
        portfolio=PortfolioSnapshot(
            account_id="collateral-account",
            initial_cash=Decimal("100000"),
            cash=Decimal("100000"),
            positions=positions,
            books=(),
            fill_count=0,
        ),
        strategy_id=strategy_id,
    )


def test_vertical_call_spread_collateral_is_exactly_the_width() -> None:
    order = multi(
        (osi(right="C", strike="500"), Side.SELL, "1"),
        (osi(right="C", strike="505"), Side.BUY, "1"),
    )
    report = option_collateral_report(order, context())
    assert report.total_collateral == Decimal("500")
    assert report.unlimited is False
    assert report.naked is False
    record = report.groups[0].contracts[0]
    assert isinstance(record, ContractCollateral)
    assert record.symbol == osi(right="C", strike="500")
    assert record.covered_by_long_option is True
    assert record.naked_contracts == ZERO

    allowed = OptionCollateralLimit({"limit": Decimal("500")}).evaluate(
        order, context()
    )
    assert allowed.action is RiskAction.ALLOW
    rejected = OptionCollateralLimit({"limit": "499.99"}).evaluate(order, context())
    assert rejected.action is RiskAction.REJECT


def test_bull_put_spread_reserves_the_width_without_nakedness() -> None:
    order = multi(
        (osi(right="P", strike="450"), Side.SELL, "1"),
        (osi(right="P", strike="440"), Side.BUY, "1"),
    )
    report = option_collateral_report(order, context())
    assert report.total_collateral == Decimal("1000")
    assert report.naked_put_contracts == ZERO
    assert NoNakedShortOptions().evaluate(order, context()).action is RiskAction.ALLOW


def test_bear_put_spread_requires_no_additional_collateral() -> None:
    order = multi(
        (osi(right="P", strike="450"), Side.SELL, "1"),
        (osi(right="P", strike="460"), Side.BUY, "1"),
    )
    report = option_collateral_report(order, context())
    assert report.total_collateral == ZERO


def test_iron_condor_reserves_exactly_the_wider_width() -> None:
    order = multi(
        (osi(right="P", strike="440"), Side.BUY, "1"),
        (osi(right="P", strike="450"), Side.SELL, "1"),
        (osi(right="C", strike="510"), Side.SELL, "1"),
        (osi(right="C", strike="520"), Side.BUY, "1"),
    )
    report = option_collateral_report(order, context())
    assert report.total_collateral == Decimal("1000")
    assert report.naked is False

    asymmetric = multi(
        (osi(right="P", strike="430"), Side.BUY, "1"),
        (osi(right="P", strike="450"), Side.SELL, "1"),
        (osi(right="C", strike="510"), Side.SELL, "1"),
        (osi(right="C", strike="515"), Side.BUY, "1"),
    )
    report = option_collateral_report(asymmetric, context())
    assert report.total_collateral == Decimal("2000")


def test_naked_short_call_is_unlimited_and_rejected() -> None:
    order = single(osi(right="C", strike="500"), Side.SELL, "1")
    report = option_collateral_report(order, context())
    assert report.unlimited is True
    assert report.total_collateral is None
    assert report.naked_call_contracts == Decimal("1")
    assert NoNakedShortOptions().evaluate(order, context()).action is RiskAction.REJECT
    assert (
        OptionCollateralLimit({"limit": "1000000"}).evaluate(order, context()).action
        is RiskAction.REJECT
    )


def test_naked_short_put_is_rejected_by_default_and_cash_secured_when_allowed() -> None:
    order = single(osi(right="P", strike="450"), Side.SELL, "1")
    report = option_collateral_report(order, context())
    assert report.total_collateral == Decimal("45000")
    assert report.naked_put_contracts == Decimal("1")
    assert NoNakedShortOptions().evaluate(order, context()).action is RiskAction.REJECT

    permissive = NoNakedShortOptions({"allow_naked_short_put": True})
    assert permissive.evaluate(order, context()).action is RiskAction.ALLOW
    assert (
        OptionCollateralLimit({"limit": "45000"}).evaluate(order, context()).action
        is RiskAction.ALLOW
    )
    assert (
        OptionCollateralLimit({"limit": "44999.99"}).evaluate(order, context()).action
        is RiskAction.REJECT
    )


def test_share_covered_calls_reserve_nothing() -> None:
    state = context(position("SPY", "100", "450"))
    order = single(osi(right="C", strike="500"), Side.SELL, "1")
    report = option_collateral_report(order, state)
    assert report.total_collateral == ZERO
    assert report.naked is False
    assert NoNakedShortOptions().evaluate(order, state).action is RiskAction.ALLOW


def test_partial_share_cover_leaves_unlimited_risk() -> None:
    state = context(position("SPY", "100", "450"))
    order = single(osi(right="C", strike="500"), Side.SELL, "2")
    report = option_collateral_report(order, state)
    assert report.unlimited is True
    assert NoNakedShortOptions().evaluate(order, state).action is RiskAction.REJECT


def test_share_cover_is_consumed_in_expiration_order() -> None:
    state = context(position("SPY", "100", "450"))
    order = multi(
        (osi(right="C", strike="500"), Side.SELL, "1"),
        (osi(expiry="241220", right="C", strike="500"), Side.SELL, "1"),
    )
    report = option_collateral_report(order, state)
    assert report.unlimited is True
    wider = context(position("SPY", "200", "450"))
    report = option_collateral_report(order, wider)
    assert report.total_collateral == ZERO

    closing = multi(
        (osi(right="C", strike="500"), Side.BUY, "1"),
        (osi(expiry="241220", right="C", strike="500"), Side.SELL, "1"),
    )
    covered = context(
        position("SPY", "100", "450"),
        position(osi(right="C", strike="500"), "-1", "2.50"),
    )
    assert NoNakedShortOptions().evaluate(closing, covered).action is RiskAction.ALLOW


def test_ratio_write_is_unlimited() -> None:
    order = multi(
        (osi(right="C", strike="490"), Side.BUY, "1"),
        (osi(right="C", strike="500"), Side.SELL, "2"),
    )
    report = option_collateral_report(order, context())
    assert report.unlimited is True
    assert NoNakedShortOptions().evaluate(order, context()).action is RiskAction.REJECT


def test_closing_and_reducing_naked_puts_pass_but_increasing_fails() -> None:
    short_two = context(position(osi(right="P", strike="450"), "-2", "4.00"))
    close_one = single(osi(right="P", strike="450"), Side.BUY, "1")
    assert (
        NoNakedShortOptions().evaluate(close_one, short_two).action is RiskAction.ALLOW
    )

    increase = single(osi(right="P", strike="450"), Side.SELL, "1")
    assert (
        NoNakedShortOptions().evaluate(increase, short_two).action is RiskAction.REJECT
    )

    short_one = context(position(osi(right="P", strike="450"), "-1", "4.00"))
    close = single(osi(right="P", strike="450"), Side.BUY, "1")
    assert NoNakedShortOptions().evaluate(close, short_one).action is RiskAction.ALLOW


def test_new_naked_group_is_not_hidden_by_an_offsetting_reduction() -> None:
    state = context(position(osi(right="P", strike="450"), "-1", "4.00"))
    order = multi(
        (osi(right="P", strike="450"), Side.BUY, "1"),
        (osi(expiry="241220", right="P", strike="440"), Side.SELL, "1"),
    )
    decision = NoNakedShortOptions().evaluate(order, state)
    assert decision.action is RiskAction.REJECT
    assert "naked short put" in (decision.reason or "")


def test_long_options_and_equity_orders_pass_through() -> None:
    long_option = single(osi(right="C", strike="500"), Side.BUY, "1")
    report = option_collateral_report(long_option, context())
    assert report.total_collateral == ZERO
    assert report.groups[0].naked_put_contracts == ZERO
    assert (
        NoNakedShortOptions().evaluate(long_option, context()).action
        is RiskAction.ALLOW
    )

    equity = single("SPY", Side.BUY, "10")
    equity_report = option_collateral_report(equity, context())
    assert equity_report.groups == ()
    assert NoNakedShortOptions().evaluate(equity, context()).action is RiskAction.ALLOW
    assert (
        OptionCollateralLimit({"limit": "0"}).evaluate(equity, context()).action
        is RiskAction.ALLOW
    )


def test_stock_leg_covers_the_call_inside_one_multi_leg_order() -> None:
    order = multi(
        ("SPY", Side.BUY, "100"),
        (osi(right="C", strike="500"), Side.SELL, "1"),
    )
    report = option_collateral_report(order, context())
    assert report.total_collateral == ZERO
    assert NoNakedShortOptions().evaluate(order, context()).action is RiskAction.ALLOW


def test_multiplier_parameter_scales_every_requirement() -> None:
    order = single(osi(right="P", strike="450"), Side.SELL, "1")
    report = option_collateral_report(order, context(), multiplier="50")
    assert report.total_collateral == Decimal("22500")
    assert (
        OptionCollateralLimit({"limit": "22500", "multiplier": "50"})
        .evaluate(order, context())
        .action
        is RiskAction.ALLOW
    )
    assert (
        OptionCollateralLimit({"limit": "22499.99", "multiplier": "50"})
        .evaluate(order, context())
        .action
        is RiskAction.REJECT
    )
    with pytest.raises(ValueError, match="multiplier"):
        option_collateral_report(order, context(), multiplier="0")


def test_strategy_override_toggles_the_naked_put_default() -> None:
    rule = NoNakedShortOptions(
        {
            "allow_naked_short_put": False,
            "per_strategy": {"alpha": {"allow_naked_short_put": True}},
        }
    )
    order = single(osi(right="P", strike="450"), Side.SELL, "1")
    assert rule.evaluate(order, context()).action is RiskAction.REJECT
    assert rule.evaluate(order, context(strategy_id="alpha")).action is RiskAction.ALLOW


def test_reports_are_deterministic_and_per_contract() -> None:
    order = multi(
        (osi(right="P", strike="450"), Side.SELL, "1"),
        (osi(right="C", strike="510"), Side.SELL, "1"),
    )
    first = option_collateral_report(order, context())
    second = option_collateral_report(order, context())
    assert first == second
    group = first.groups[0]
    assert group.root == "SPY"
    assert group.expiration == EXPIRY
    by_symbol = {record.symbol: record for record in group.contracts}
    call_symbol = osi(right="C", strike="510")
    put_symbol = osi(right="P", strike="450")
    assert by_symbol[call_symbol].projected_contracts == Decimal("-1")
    assert by_symbol[call_symbol].naked_contracts == Decimal("1")
    assert by_symbol[put_symbol].naked_contracts == Decimal("1")

    decision = NoNakedShortOptions().evaluate(order, context())
    assert decision.action is RiskAction.REJECT
    metrics = decision.metrics
    assert metrics["unlimited"] is True
    groups = metrics["groups"]
    assert isinstance(groups, Mapping)
    group_metrics = groups["SPY 2024-11-19"]
    assert isinstance(group_metrics, Mapping)
    contracts = group_metrics["contracts"]
    assert isinstance(contracts, Mapping)
    assert call_symbol in contracts
    assert put_symbol in contracts


def test_pipeline_composes_both_rules_with_complete_audits() -> None:
    pipeline = RiskPipeline(
        (NoNakedShortOptions(), OptionCollateralLimit({"limit": "100000"}))
    )
    condor = multi(
        (osi(right="P", strike="440"), Side.BUY, "1"),
        (osi(right="P", strike="450"), Side.SELL, "1"),
        (osi(right="C", strike="510"), Side.SELL, "1"),
        (osi(right="C", strike="520"), Side.BUY, "1"),
        order_id="condor-1",
    )
    decision = pipeline.evaluate(condor, context())
    assert decision.action is RiskAction.ALLOW
    assert [audit.rule_id for audit in decision.audits] == [
        "NoNakedShortOptions",
        "OptionCollateralLimit",
    ]
    assert all(audit.metrics for audit in decision.audits)

    naked = single(osi(right="C", strike="500"), Side.SELL, "1", order_id="naked-1")
    rejected = pipeline.evaluate(naked, context())
    assert rejected.action is RiskAction.REJECT
    assert rejected.reasons[0] == (
        "no_naked_short_options leaves unlimited short option exposure"
    )


def test_rules_satisfy_the_risk_rule_protocol() -> None:
    assert isinstance(NoNakedShortOptions(), RiskRule)
    assert isinstance(OptionCollateralLimit({"limit": "1"}), RiskRule)
    with pytest.raises(ValueError, match="allow_naked_short_put"):
        NoNakedShortOptions({"allow_naked_short_put": "yes"}).evaluate(
            single(osi(right="P", strike="450"), Side.SELL, "1"), context()
        )
    with pytest.raises(ValueError, match="limit"):
        OptionCollateralLimit({}).evaluate(single("SPY", Side.BUY, "1"), context())
