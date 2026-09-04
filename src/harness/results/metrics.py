"""Pure, deterministic metrics for backtest results.

This module computes standard performance metrics and per-strategy
attribution from plain data (frozen dataclasses and ``Decimal`` values).
It performs no I/O, reads no wall clock, draws no randomness, and touches
no network: every function is a pure mapping from its arguments to plain
data, so the same inputs always produce identical outputs.

Conventions
-----------

Money and fixed point
    All monetary and ratio arithmetic uses :class:`decimal.Decimal` with
    the default (28-digit) context; results are exact where the arithmetic
    allows and correctly rounded by the context where it does not.

Float policy
    Only two final annualization steps leave ``Decimal``: the square root
    in Sharpe/Sortino annualization and the fractional exponent in CAGR.
    Both convert the exact ``Decimal`` intermediate to ``float`` and apply
    ``math.sqrt``/``**`` there; relative tolerance versus an exact-real
    computation is bounded by float64 rounding (about 1e-15 relative).
    No intermediate arithmetic is performed in floating point.

Annualization
    Equity-curve CAGR uses the 365-day year convention: one year equals
    ``365 * 86_400 * 1_000_000_000`` nanoseconds, and CAGR is
    ``(end_equity / start_equity) ** (one_year_ns / span_ns)``.
    Return-series metrics take an explicit ``periods_per_year`` integer
    and annualize linear ratios by ``sqrt(periods_per_year)``.

Undefined values
    Metrics that are genuinely undefined return ``None`` rather than a
    sentinel number: ``total_return``/``CAGR`` need at least two equity
    points (CAGR additionally needs a positive start equity, a positive
    span, and a positive growth ratio), Sharpe needs at least two returns
    and a nonzero standard deviation, Sortino needs at least one strictly
    negative return (a downside deviation of zero), and the profit factor
    is ``None`` whenever the gross loss is zero (including the no-loss
    and no-trades cases). Empty ledgers yield zero counts and ``None``
    aggregates rather than errors. Equity values must be non-negative;
    a curve that ends at zero reports a max drawdown of exactly 1.

Aggregation details
    ``max_drawdown`` is ``1 - trough/peak`` against the running peak, is
    a non-negative ``Decimal``, and is ``0`` when equity never falls
    below its running peak. Drawdown duration is the longest peak to
    recovery span in nanoseconds; ``DrawdownSpan.recovered`` is ``False``
    when the longest span has not recovered by the final point.
    ``hit_rate`` counts strictly positive realized trade PnL values over
    all trades (zero-PnL trades count in the denominator, not as wins).
    ``profit_factor`` is gross profit over gross loss magnitude, where
    the gross loss magnitude is ``None``-safe: it is reported as a
    non-negative ``Decimal`` and the ratio is ``None`` without losses.
    Fill ``slippage`` is the signed per-fill cost
    ``quantity * (fill_price - reference_price)`` from
    :func:`fill_slippage`; sells carry negative quantity. Order outcomes
    classify the exact lowercase strings ``"filled"``, ``"rejected"``,
    and ``"cancelled"``; every other value counts as ``other``. Latency
    aggregates cover fills with a non-``None`` ``latency_ns`` only.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_NANOS_PER_DAY: Final = 86_400_000_000_000
_NANOS_PER_YEAR_365: Final = 365 * _NANOS_PER_DAY
_FILLED: Final = "filled"
_REJECTED: Final = "rejected"
_CANCELLED: Final = "cancelled"


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One equity mark: ``timestamp_ns`` in ascending order across points."""

    timestamp_ns: int
    equity: Decimal


@dataclass(frozen=True, slots=True)
class TradeRow:
    """One realized trade; ``realized_pnl`` is the signed Decimal PnL."""

    trade_id: str
    strategy_id: str
    realized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class FillRow:
    """One fill ledger row; ``slippage`` is the signed per-fill cost."""

    fill_id: str
    order_id: str
    strategy_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    slippage: Decimal
    latency_ns: int | None = None


@dataclass(frozen=True, slots=True)
class OrderRow:
    """One order ledger row; ``outcome`` is the exact lowercase status."""

    order_id: str
    strategy_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class DrawdownSpan:
    """Longest drawdown span; unrecovered spans run to the final point."""

    duration_ns: int
    recovered: bool


@dataclass(frozen=True, slots=True)
class EquityMetrics:
    """Aggregate metrics over one equity curve."""

    total_return: Decimal | None
    cagr: float | None
    max_drawdown: Decimal
    drawdown: DrawdownSpan


@dataclass(frozen=True, slots=True)
class TradeStats:
    """Aggregates over realized trade PnL values."""

    trade_count: int
    win_count: int
    loss_count: int
    hit_rate: Decimal | None
    total_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    largest_win: Decimal | None
    largest_loss: Decimal | None


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Latency aggregates over fills carrying a non-None ``latency_ns``."""

    count: int
    mean_ns: Decimal | None
    max_ns: int | None


@dataclass(frozen=True, slots=True)
class OrderOutcomeCounts:
    """Order ledger counts by exact lowercase outcome string."""

    total: int
    filled: int
    rejected: int
    cancelled: int
    other: int


@dataclass(frozen=True, slots=True)
class ExecutionStats:
    """Execution aggregates over one fill/order ledger pair."""

    fill_count: int
    fees_total: Decimal
    slippage_total: Decimal
    latency: LatencyStats
    outcomes: OrderOutcomeCounts


@dataclass(frozen=True, slots=True)
class StrategyAttribution:
    """Per-strategy view of trade and execution aggregates."""

    strategy_id: str
    trades: TradeStats
    execution: ExecutionStats


def _equity_points(points: Sequence[EquityPoint]) -> tuple[EquityPoint, ...]:
    prepared = tuple(points)
    for index, point in enumerate(prepared):
        if not isinstance(point, EquityPoint):
            raise TypeError("points must contain EquityPoint values")
        if point.equity < _ZERO:
            raise ValueError("equity values must be non-negative")
        if index > 0 and point.timestamp_ns < prepared[index - 1].timestamp_ns:
            raise ValueError("equity points must be in ascending order")
    return prepared


def fill_slippage(
    quantity: Decimal, reference_price: Decimal, fill_price: Decimal
) -> Decimal:
    """Return the signed slippage of one fill against its reference price.

    Buys carry positive ``quantity`` and sells negative quantity, so
    favorable sells still contribute positive slippage cost when the fill
    price lands below the reference.
    """
    return quantity * (fill_price - reference_price)


def total_return(points: Sequence[EquityPoint]) -> Decimal | None:
    """Return ``last/first - 1`` over the curve, or ``None`` if undefined.

    Undefined when fewer than two points are supplied or the first equity
    is zero.
    """
    prepared = _equity_points(points)
    if len(prepared) < 2 or prepared[0].equity == _ZERO:
        return None
    return prepared[-1].equity / prepared[0].equity - _ONE


def cagr(points: Sequence[EquityPoint]) -> float | None:
    """Return the 365-day-year CAGR, or ``None`` if undefined.

    Undefined when fewer than two points are supplied, the span is not
    positive, the start equity is not positive, or the growth ratio is
    not positive (a total wipeout is reported by the drawdown instead).
    The exact ``Decimal`` growth ratio and span feed one final float
    exponentiation (relative tolerance about 1e-15).
    """
    prepared = _equity_points(points)
    if len(prepared) < 2:
        return None
    start, end = prepared[0], prepared[-1]
    span = end.timestamp_ns - start.timestamp_ns
    if span <= 0 or start.equity <= _ZERO:
        return None
    growth = end.equity / start.equity
    if growth <= _ZERO:
        return None
    years = Decimal(span) / Decimal(_NANOS_PER_YEAR_365)
    return math.pow(float(growth), float(_ONE / years))


def max_drawdown(points: Sequence[EquityPoint]) -> Decimal:
    """Return the largest ``1 - trough/peak`` as a non-negative Decimal.

    Returns ``0`` when the curve never falls below its running peak (and
    for single-point curves).
    """
    prepared = _equity_points(points)
    peak = prepared[0].equity if prepared else _ZERO
    worst = _ZERO
    for point in prepared:
        if point.equity > peak:
            peak = point.equity
        elif peak > _ZERO and point.equity < peak:
            worst = max(worst, _ONE - point.equity / peak)
    return worst


def longest_drawdown(points: Sequence[EquityPoint]) -> DrawdownSpan:
    """Return the longest peak-to-recovery span of the equity curve.

    Ties prefer the recovered span. A single-point curve and a curve that
    never dips report a zero-length recovered span; an all-loss ending
    reports the open span from the last peak with ``recovered=False``.
    """
    prepared = _equity_points(points)
    if len(prepared) < 2:
        return DrawdownSpan(duration_ns=0, recovered=True)
    peak = prepared[0].equity
    peak_ts = prepared[0].timestamp_ns
    best_closed = 0
    open_peak_ts: int | None = None
    for point in prepared[1:]:
        if point.equity >= peak:
            if open_peak_ts is not None:
                best_closed = max(best_closed, point.timestamp_ns - open_peak_ts)
                open_peak_ts = None
            peak, peak_ts = point.equity, point.timestamp_ns
        elif open_peak_ts is None:
            open_peak_ts = peak_ts
    if open_peak_ts is not None:
        open_span = prepared[-1].timestamp_ns - open_peak_ts
        if open_span > best_closed:
            return DrawdownSpan(duration_ns=open_span, recovered=False)
    return DrawdownSpan(duration_ns=best_closed, recovered=True)


def equity_curve_metrics(points: Sequence[EquityPoint]) -> EquityMetrics:
    """Return every equity-curve metric for one curve at once."""
    return EquityMetrics(
        total_return=total_return(points),
        cagr=cagr(points),
        max_drawdown=max_drawdown(points),
        drawdown=longest_drawdown(points),
    )


def trade_stats(trades: Sequence[TradeRow]) -> TradeStats:
    """Aggregate realized trade PnL values.

    Wins are strictly positive PnL values and losses strictly negative;
    zero-PnL trades count in ``trade_count`` but in neither bucket, so
    ``hit_rate = win_count / trade_count``. ``gross_loss`` is the
    non-negative loss magnitude and ``profit_factor = gross_profit /
    gross_loss`` is ``None`` when there are no losses.
    """
    rows = tuple(trades)
    total = _ZERO
    gross_profit = _ZERO
    gross_loss = _ZERO
    wins: list[Decimal] = []
    losses: list[Decimal] = []
    for row in rows:
        pnl = row.realized_pnl
        total += pnl
        if pnl > _ZERO:
            wins.append(pnl)
            gross_profit += pnl
        elif pnl < _ZERO:
            losses.append(pnl)
            gross_loss -= pnl
    trade_count = len(rows)
    average_win = sum(wins, _ZERO) / len(wins) if wins else None
    average_loss = sum(losses, _ZERO) / len(losses) if losses else None
    return TradeStats(
        trade_count=trade_count,
        win_count=len(wins),
        loss_count=len(losses),
        hit_rate=Decimal(len(wins)) / Decimal(trade_count) if trade_count else None,
        total_pnl=total,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=gross_profit / gross_loss if gross_loss > _ZERO else None,
        average_win=average_win,
        average_loss=average_loss,
        largest_win=max(wins) if wins else None,
        largest_loss=min(losses) if losses else None,
    )


def sharpe(returns: Sequence[Decimal], periods_per_year: int) -> float | None:
    """Return the annualized Sharpe ratio, or ``None`` if undefined.

    ``mean(return) / population_std(return) * sqrt(periods_per_year)``
    with the standard deviation over the same sample (ddof=0). Undefined
    (``None``) when fewer than two returns are supplied or the standard
    deviation is zero; the single ``math.sqrt`` float conversion bounds
    the tolerance at about 1e-15 relative.
    """
    values = tuple(returns)
    if len(values) < 2 or periods_per_year <= 0:
        return None
    count = Decimal(len(values))
    mean = sum(values, _ZERO) / count
    variance = sum(((value - mean) ** 2 for value in values), _ZERO) / count
    if variance == _ZERO:
        return None
    return float(mean / variance.sqrt()) * math.sqrt(periods_per_year)


def sortino(
    returns: Sequence[Decimal], periods_per_year: int, target: Decimal = _ZERO
) -> float | None:
    """Return the annualized Sortino ratio, or ``None`` if undefined.

    ``mean(return - target) / downside_deviation * sqrt(periods_per_year)``
    where the downside deviation is the square root of the mean squared
    shortfall below ``target``. Undefined (``None``) when no return falls
    strictly below the target (zero downside deviation).
    """
    values = tuple(returns)
    if not values or periods_per_year <= 0:
        return None
    shortfalls = [(value - target) for value in values if value < target]
    if not shortfalls:
        return None
    count = Decimal(len(values))
    mean = sum(values, _ZERO) / count
    variance = sum((shortfall**2 for shortfall in shortfalls), _ZERO) / count
    if variance == _ZERO:
        return None
    return float(mean / variance.sqrt()) * math.sqrt(periods_per_year)


def latency_stats(fills: Sequence[FillRow]) -> LatencyStats:
    """Aggregate fill latencies, ignoring ``None`` latency rows."""
    samples = tuple(fill.latency_ns for fill in fills if fill.latency_ns is not None)
    if not samples:
        return LatencyStats(count=0, mean_ns=None, max_ns=None)
    count = Decimal(len(samples))
    return LatencyStats(
        count=len(samples),
        mean_ns=Decimal(sum(samples)) / count,
        max_ns=max(samples),
    )


def order_outcome_counts(orders: Sequence[OrderRow]) -> OrderOutcomeCounts:
    """Count order outcomes by exact lowercase classification."""
    counts = {_FILLED: 0, _REJECTED: 0, _CANCELLED: 0, "other": 0}
    for order in orders:
        key = order.outcome if order.outcome in counts else "other"
        counts[key] += 1
    return OrderOutcomeCounts(
        total=len(orders),
        filled=counts[_FILLED],
        rejected=counts[_REJECTED],
        cancelled=counts[_CANCELLED],
        other=counts["other"],
    )


def execution_stats(
    fills: Sequence[FillRow], orders: Sequence[OrderRow]
) -> ExecutionStats:
    """Aggregate fees, slippage, latency, and outcomes over the ledgers."""
    rows = tuple(fills)
    return ExecutionStats(
        fill_count=len(rows),
        fees_total=sum((fill.fee for fill in rows), _ZERO),
        slippage_total=sum((fill.slippage for fill in rows), _ZERO),
        latency=latency_stats(rows),
        outcomes=order_outcome_counts(orders),
    )


def per_strategy_attribution(
    fills: Sequence[FillRow] = (),
    orders: Sequence[OrderRow] = (),
    trades: Sequence[TradeRow] = (),
) -> dict[str, StrategyAttribution]:
    """Return the trade and execution aggregates for each strategy.

    Strategy IDs are the union of the strategies appearing in any input
    ledger, returned in sorted order; a strategy present only in one
    ledger gets zeroed aggregates for the others.
    """
    grouped_fills: dict[str, list[FillRow]] = {}
    for fill in fills:
        grouped_fills.setdefault(fill.strategy_id, []).append(fill)
    grouped_orders: dict[str, list[OrderRow]] = {}
    for order in orders:
        grouped_orders.setdefault(order.strategy_id, []).append(order)
    grouped_trades: dict[str, list[TradeRow]] = {}
    for trade in trades:
        grouped_trades.setdefault(trade.strategy_id, []).append(trade)
    return {
        strategy_id: StrategyAttribution(
            strategy_id=strategy_id,
            trades=trade_stats(grouped_trades.get(strategy_id, ())),
            execution=execution_stats(
                grouped_fills.get(strategy_id, ()),
                grouped_orders.get(strategy_id, ()),
            ),
        )
        for strategy_id in sorted(
            set(grouped_fills) | set(grouped_orders) | set(grouped_trades)
        )
    }


__all__ = [
    "DrawdownSpan",
    "EquityMetrics",
    "EquityPoint",
    "ExecutionStats",
    "FillRow",
    "LatencyStats",
    "OrderOutcomeCounts",
    "OrderRow",
    "StrategyAttribution",
    "TradeRow",
    "TradeStats",
    "cagr",
    "equity_curve_metrics",
    "execution_stats",
    "fill_slippage",
    "latency_stats",
    "longest_drawdown",
    "max_drawdown",
    "order_outcome_counts",
    "per_strategy_attribution",
    "sharpe",
    "sortino",
    "total_return",
    "trade_stats",
]
