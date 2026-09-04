"""Focused tests for the pure metrics and attribution module."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Final

import pytest

from harness.results.metrics import (
    EquityPoint,
    FillRow,
    OrderRow,
    TradeRow,
    cagr,
    equity_curve_metrics,
    execution_stats,
    fill_slippage,
    latency_stats,
    longest_drawdown,
    max_drawdown,
    order_outcome_counts,
    per_strategy_attribution,
    sharpe,
    sortino,
    total_return,
    trade_stats,
)

D: Final = Decimal
NS_PER_DAY: Final = 86_400_000_000_000


def points(*values: str) -> list[EquityPoint]:
    """Build a daily equity curve from decimal string marks."""
    return [
        EquityPoint(timestamp_ns=day * NS_PER_DAY, equity=D(value))
        for day, value in enumerate(values)
    ]


def make_fill(
    fill_id: str,
    *,
    strategy_id: str = "alpha",
    fee: str = "0",
    slippage: str = "0",
    latency_ns: int | None = 0,
) -> FillRow:
    """Build one fill row with deterministic money fields."""
    return FillRow(
        fill_id=fill_id,
        order_id=f"order-{fill_id}",
        strategy_id=strategy_id,
        quantity=D("1"),
        price=D("100"),
        fee=D(fee),
        slippage=D(slippage),
        latency_ns=latency_ns,
    )


def make_order(order_id: str, outcome: str, *, strategy_id: str = "alpha") -> OrderRow:
    """Build one order row with the given outcome."""
    return OrderRow(order_id=order_id, strategy_id=strategy_id, outcome=outcome)


def make_trade(
    pnl: str, *, strategy_id: str = "alpha", trade_id: str = "t"
) -> TradeRow:
    """Build one trade row with the given realized PnL."""
    return TradeRow(trade_id=trade_id, strategy_id=strategy_id, realized_pnl=D(pnl))


def test_total_return_two_point_curve() -> None:
    assert total_return(points("100", "80", "121")) == D("0.21")


def test_total_return_single_point_and_zero_start_are_none() -> None:
    assert total_return(points("100")) is None
    assert total_return(points("0", "50")) is None
    assert total_return([]) is None


def test_cagr_exact_one_year_double() -> None:
    year = [EquityPoint(0, D("100")), EquityPoint(365 * NS_PER_DAY, D("121"))]
    assert cagr(year) == pytest.approx(1.21)
    double = [
        EquityPoint(0, D("100")),
        EquityPoint(NS_PER_DAY, D("105")),
        EquityPoint(365 * NS_PER_DAY, D("121")),
    ]
    assert cagr(double) == pytest.approx(1.21)


def test_cagr_partial_year_compounds_annually() -> None:
    half = [EquityPoint(0, D("100")), EquityPoint(NS_PER_DAY * 365 // 2, D("121"))]
    assert cagr(half) == pytest.approx(1.21**2)


def test_cagr_undefined_cases() -> None:
    assert cagr(points("100")) is None
    assert cagr(points("0", "10")) is None
    assert cagr(points("100", "0")) is None
    same_time = [EquityPoint(5, D("100")), EquityPoint(5, D("121"))]
    assert cagr(same_time) is None


def test_max_drawdown_hand_computed() -> None:
    curve = points("100", "80", "120", "90")
    assert max_drawdown(curve) == D("0.25")


def test_max_drawdown_never_below_peak_is_zero() -> None:
    assert max_drawdown(points("100", "120", "121")) == D("0")
    assert max_drawdown(points("100")) == D("0")


def test_max_drawdown_all_loss_series_is_one() -> None:
    assert max_drawdown(points("100", "50", "0")) == D("1")


def test_max_drawdown_rejects_negative_equity_and_out_of_order() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        max_drawdown(points("100", "-1"))
    with pytest.raises(ValueError, match="ascending"):
        max_drawdown([EquityPoint(2, D("100")), EquityPoint(1, D("100"))])


def test_drawdown_duration_recovered_span() -> None:
    span = longest_drawdown(points("100", "80", "120", "90"))
    assert span.duration_ns == 2 * NS_PER_DAY
    assert span.recovered is True


def test_drawdown_duration_open_span_flagged_unrecovered() -> None:
    span = longest_drawdown(points("100", "50", "0"))
    assert span.duration_ns == 2 * NS_PER_DAY
    assert span.recovered is False


def test_drawdown_duration_prefers_longer_closed_span() -> None:
    curve = points("100", "90", "100", "99")
    span = longest_drawdown(curve)
    assert span.duration_ns == 2 * NS_PER_DAY
    assert span.recovered is True


def test_drawdown_duration_single_point_and_flat() -> None:
    assert longest_drawdown(points("100")).recovered is True
    flat = longest_drawdown(points("100", "100", "100"))
    assert flat.duration_ns == 0
    assert flat.recovered is True


def test_equity_curve_metrics_bundle_matches_parts() -> None:
    curve = points("100", "80", "120", "90")
    metrics = equity_curve_metrics(curve)
    assert metrics.total_return == total_return(curve)
    assert metrics.cagr == cagr(curve)
    assert metrics.max_drawdown == max_drawdown(curve)
    assert metrics.drawdown == longest_drawdown(curve)


def test_trade_stats_hand_computed() -> None:
    stats = trade_stats(
        [
            make_trade("100", trade_id="t1"),
            make_trade("-50", trade_id="t2"),
            make_trade("30", trade_id="t3"),
            make_trade("-20", trade_id="t4"),
            make_trade("0", trade_id="t5"),
        ]
    )
    assert stats.trade_count == 5
    assert stats.win_count == 2
    assert stats.loss_count == 2
    assert stats.hit_rate == D("2") / D("5")
    assert stats.total_pnl == D("60")
    assert stats.gross_profit == D("130")
    assert stats.gross_loss == D("70")
    assert stats.profit_factor == D("130") / D("70")
    assert stats.average_win == D("65")
    assert stats.average_loss == D("-35")
    assert stats.largest_win == D("100")
    assert stats.largest_loss == D("-50")


def test_trade_stats_no_losses_profit_factor_none() -> None:
    stats = trade_stats([make_trade("10"), make_trade("5")])
    assert stats.gross_loss == D("0")
    assert stats.profit_factor is None
    assert stats.hit_rate == D("1")


def test_trade_stats_all_losses_and_empty() -> None:
    stats = trade_stats([make_trade("-5"), make_trade("-5")])
    assert stats.hit_rate == D("0")
    assert stats.profit_factor == D("0")
    assert stats.total_pnl == D("-10")
    empty = trade_stats([])
    assert empty.trade_count == 0
    assert empty.hit_rate is None
    assert empty.profit_factor is None
    assert empty.total_pnl == D("0")


def test_sharpe_hand_computed() -> None:
    returns = [D("0.05"), D("0.05"), D("-0.01"), D("-0.01")]
    expected = (2.0 / 3.0) * math.sqrt(252)
    assert sharpe(returns, 252) == pytest.approx(expected)


def test_sharpe_zero_variance_and_short_series_are_none() -> None:
    assert sharpe([D("0.02"), D("0.02")], 252) is None
    assert sharpe([D("0.02")], 252) is None
    assert sharpe([], 252) is None


def test_sortino_hand_computed() -> None:
    returns = [D("0.03"), D("-0.01"), D("0.05"), D("-0.03")]
    mean = D("0.01")
    downside = ((D("0.0001") + D("0.0009")) / D("4")).sqrt()
    assert sortino(returns, 4) == pytest.approx(float(mean / downside * D("2")))


def test_sortino_no_downside_is_none() -> None:
    assert sortino([D("0.01"), D("0.02")], 4) is None
    assert sortino([D("0"), D("0")], 4) is None


def test_latency_stats_hand_computed_and_skips_none() -> None:
    fills = [
        make_fill("f1", latency_ns=100),
        make_fill("f2", latency_ns=200),
        make_fill("f3", latency_ns=None),
        make_fill("f4", latency_ns=300),
    ]
    stats = latency_stats(fills)
    assert stats.count == 3
    assert stats.mean_ns == D("200")
    assert stats.max_ns == 300


def test_latency_stats_empty_and_all_none() -> None:
    assert latency_stats([]) == latency_stats([make_fill("f1", latency_ns=None)])
    stats = latency_stats([make_fill("f1", latency_ns=None)])
    assert stats.count == 0
    assert stats.mean_ns is None
    assert stats.max_ns is None


def test_order_outcome_counts_hand_computed() -> None:
    orders = [
        make_order("o1", "filled"),
        make_order("o2", "filled"),
        make_order("o3", "rejected"),
        make_order("o4", "cancelled"),
        make_order("o5", "expired"),
    ]
    counts = order_outcome_counts(orders)
    assert counts.total == 5
    assert counts.filled == 2
    assert counts.rejected == 1
    assert counts.cancelled == 1
    assert counts.other == 1


def test_order_outcome_counts_empty() -> None:
    counts = order_outcome_counts([])
    assert counts.total == 0
    assert counts.filled == 0
    assert counts.rejected == 0
    assert counts.cancelled == 0
    assert counts.other == 0


def test_execution_stats_aggregates_fees_slippage_and_outcomes() -> None:
    fills = [
        make_fill("f1", fee="1.5", slippage="1.0", latency_ns=10),
        make_fill("f2", fee="2.5", slippage="0.2", latency_ns=30),
    ]
    orders = [make_order("o1", "filled"), make_order("o2", "rejected")]
    stats = execution_stats(fills, orders)
    assert stats.fill_count == 2
    assert stats.fees_total == D("4.0")
    assert stats.slippage_total == D("1.2")
    assert stats.latency.mean_ns == D("20")
    assert stats.outcomes.total == 2


def test_execution_stats_empty_ledgers() -> None:
    stats = execution_stats([], [])
    assert stats.fill_count == 0
    assert stats.fees_total == D("0")
    assert stats.slippage_total == D("0")
    assert stats.latency.count == 0
    assert stats.latency.mean_ns is None
    assert stats.outcomes.total == 0


def test_slippage_reconciliation_against_ledger_cost_components() -> None:
    ledger = [
        ("f1", D("2"), D("100"), D("100.5")),
        ("f2", D("1"), D("50"), D("50.4")),
        ("f3", D("-3"), D("200"), D("199.5")),
    ]
    components = [
        fill_slippage(quantity, reference, fill_price)
        for _, quantity, reference, fill_price in ledger
    ]
    fills = [
        FillRow(
            fill_id=fill_id,
            order_id=f"order-{fill_id}",
            strategy_id="alpha",
            quantity=quantity,
            price=fill_price,
            fee=D("0"),
            slippage=fill_slippage(quantity, reference, fill_price),
        )
        for fill_id, quantity, reference, fill_price in ledger
    ]
    assert components[0] == D("1.0")
    assert components[2] == D("1.5")
    total = sum(components, D("0"))
    assert abs(execution_stats(fills, []).slippage_total - total) <= D("1e-12")


def test_per_strategy_attribution_groups_all_ledgers() -> None:
    fills = [
        make_fill("f1", strategy_id="alpha", fee="1", slippage="0.5", latency_ns=10),
        make_fill("f2", strategy_id="beta", fee="2", latency_ns=20),
    ]
    orders = [make_order("o1", "filled", strategy_id="alpha")]
    trades = [make_trade("10", strategy_id="alpha", trade_id="t1")]
    attribution = per_strategy_attribution(fills, orders, trades)

    assert list(attribution) == ["alpha", "beta"]
    alpha = attribution["alpha"]
    assert alpha.trades.total_pnl == D("10")
    assert alpha.trades.profit_factor is None
    assert alpha.execution.fees_total == D("1")
    assert alpha.execution.slippage_total == D("0.5")
    assert alpha.execution.outcomes.filled == 1
    beta = attribution["beta"]
    assert beta.trades.trade_count == 0
    assert beta.trades.hit_rate is None
    assert beta.execution.fees_total == D("2")
    assert beta.execution.outcomes.total == 0


def test_per_strategy_attribution_empty_inputs() -> None:
    assert per_strategy_attribution() == {}
