//! Pure, deterministic metrics for backtest results.
//!
//! Port of `harness.results.metrics`. Every function is a pure mapping from
//! plain data to plain data: no I/O, wall clock, randomness, or network. Money
//! and ratio arithmetic uses the scale-18 fixed-point kernel; the two final
//! annualization steps (the Sharpe/Sortino square root and the CAGR fractional
//! exponent) convert to `f64` and bound the tolerance at float64 rounding.
//!
//! Undefined values are represented as `None` rather than a sentinel: fewer
//! than two equity points for `total_return`/CAGR, a zero start equity, a zero
//! standard deviation for Sharpe, a zero downside deviation for Sortino, and a
//! zero gross loss for the profit factor.

use std::collections::BTreeMap;

use harness_num::Decimal18;

/// Nanoseconds per day.
pub const NANOS_PER_DAY: i64 = 86_400_000_000_000;
/// Nanoseconds per 365-day year.
pub const NANOS_PER_YEAR_365: i64 = 365 * NANOS_PER_DAY;

fn dec(value: &str) -> Decimal18 {
    value.parse::<Decimal18>().unwrap_or(Decimal18::ZERO)
}

/// One equity mark; timestamps ascend across points.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EquityPoint {
    /// Mark time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Mark equity.
    pub equity: Decimal18,
}

/// One realized trade.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TradeRow {
    /// Trade identifier.
    pub trade_id: String,
    /// Strategy identifier.
    pub strategy_id: String,
    /// Signed realized PnL.
    pub realized_pnl: Decimal18,
}

/// One fill ledger row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FillRow {
    /// Fill identifier.
    pub fill_id: String,
    /// Order identifier.
    pub order_id: String,
    /// Strategy identifier.
    pub strategy_id: String,
    /// Signed quantity.
    pub quantity: Decimal18,
    /// Fill price.
    pub price: Decimal18,
    /// Fee.
    pub fee: Decimal18,
    /// Signed per-fill slippage.
    pub slippage: Decimal18,
    /// Optional latency.
    pub latency_ns: Option<i64>,
}

/// One order ledger row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OrderRow {
    /// Order identifier.
    pub order_id: String,
    /// Strategy identifier.
    pub strategy_id: String,
    /// Exact lowercase outcome status.
    pub outcome: String,
}

/// Longest drawdown span.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DrawdownSpan {
    /// Span length in nanoseconds.
    pub duration_ns: i64,
    /// Whether the span recovered by the final point.
    pub recovered: bool,
}

/// Aggregate metrics over one equity curve.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EquityMetrics {
    /// `last/first - 1`, if defined.
    pub total_return: Option<Decimal18>,
    /// 365-day CAGR as a float, if defined.
    pub cagr: Option<f64>,
    /// Largest `1 - trough/peak`.
    pub max_drawdown: Decimal18,
    /// Longest peak-to-recovery span.
    pub drawdown: DrawdownSpan,
}

/// Aggregates over realized trade PnL values.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TradeStats {
    /// Number of trades.
    pub trade_count: usize,
    /// Number of winning trades.
    pub win_count: usize,
    /// Number of losing trades.
    pub loss_count: usize,
    /// Win share, if there are trades.
    pub hit_rate: Option<Decimal18>,
    /// Sum of PnL.
    pub total_pnl: Decimal18,
    /// Sum of positive PnL.
    pub gross_profit: Decimal18,
    /// Loss magnitude.
    pub gross_loss: Decimal18,
    /// Profit factor, if there are losses.
    pub profit_factor: Option<Decimal18>,
    /// Mean win, if any.
    pub average_win: Option<Decimal18>,
    /// Mean loss, if any.
    pub average_loss: Option<Decimal18>,
    /// Largest win, if any.
    pub largest_win: Option<Decimal18>,
    /// Largest loss, if any.
    pub largest_loss: Option<Decimal18>,
}

/// Latency aggregates over fills carrying a non-`None` latency.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LatencyStats {
    /// Number of samples.
    pub count: usize,
    /// Mean latency, if any.
    pub mean_ns: Option<Decimal18>,
    /// Maximum latency, if any.
    pub max_ns: Option<i64>,
}

/// Order ledger counts by exact lowercase outcome.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OrderOutcomeCounts {
    /// Total orders.
    pub total: usize,
    /// Filled count.
    pub filled: usize,
    /// Rejected count.
    pub rejected: usize,
    /// Cancelled count.
    pub cancelled: usize,
    /// Every other outcome count.
    pub other: usize,
}

/// Execution aggregates over one fill/order ledger pair.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ExecutionStats {
    /// Number of fills.
    pub fill_count: usize,
    /// Total fees.
    pub fees_total: Decimal18,
    /// Total slippage.
    pub slippage_total: Decimal18,
    /// Latency aggregates.
    pub latency: LatencyStats,
    /// Order outcome counts.
    pub outcomes: OrderOutcomeCounts,
}

/// Per-strategy view of trade and execution aggregates.
#[derive(Clone, Debug, PartialEq)]
pub struct StrategyAttribution {
    /// Strategy identifier.
    pub strategy_id: String,
    /// Trade aggregates.
    pub trades: TradeStats,
    /// Execution aggregates.
    pub execution: ExecutionStats,
}

/// Return the signed slippage of one fill against its reference price.
#[must_use]
pub fn fill_slippage(
    quantity: Decimal18,
    reference_price: Decimal18,
    fill_price: Decimal18,
) -> Decimal18 {
    quantity
        .checked_mul(
            fill_price
                .checked_sub(reference_price)
                .unwrap_or(Decimal18::ZERO),
        )
        .unwrap_or(Decimal18::ZERO)
}

fn validate_points(points: &[EquityPoint]) -> Result<(), &'static str> {
    for (index, point) in points.iter().enumerate() {
        if point.equity.is_negative() {
            return Err("equity values must be non-negative");
        }
        if index > 0 && point.timestamp_ns < points[index - 1].timestamp_ns {
            return Err("equity points must be in ascending order");
        }
    }
    Ok(())
}

/// Return `last/first - 1` over the curve, or `None` if undefined.
#[must_use]
pub fn total_return(points: &[EquityPoint]) -> Option<Decimal18> {
    if validate_points(points).is_err() || points.len() < 2 || points[0].equity.is_zero() {
        return None;
    }
    points[points.len() - 1]
        .equity
        .checked_div(points[0].equity)
        .ok()
        .and_then(|ratio| ratio.checked_sub(Decimal18::ONE).ok())
}

/// Return the 365-day-year CAGR, or `None` if undefined.
#[must_use]
pub fn cagr(points: &[EquityPoint]) -> Option<f64> {
    if validate_points(points).is_err() || points.len() < 2 {
        return None;
    }
    let start = points[0];
    let end = points[points.len() - 1];
    let span = end.timestamp_ns - start.timestamp_ns;
    if span <= 0 || !start.equity.is_positive() {
        return None;
    }
    let growth = end.equity.checked_div(start.equity).ok()?;
    if !growth.is_positive() {
        return None;
    }
    let years = Decimal18::from_i64(span)
        .checked_div(Decimal18::from_i64(NANOS_PER_YEAR_365))
        .ok()?;
    let exponent = 1.0 / years.to_f64();
    Some(growth.to_f64().powf(exponent))
}

/// Return the largest `1 - trough/peak` as a non-negative decimal.
#[must_use]
pub fn max_drawdown(points: &[EquityPoint]) -> Decimal18 {
    if validate_points(points).is_err() {
        return Decimal18::ZERO;
    }
    let mut peak = points.first().map_or(Decimal18::ZERO, |point| point.equity);
    let mut worst = Decimal18::ZERO;
    for point in points {
        if point.equity > peak {
            peak = point.equity;
        } else if peak.is_positive() && point.equity.less_than(peak) {
            if let Ok(ratio) = point.equity.checked_div(peak) {
                if let Ok(dip) = Decimal18::ONE.checked_sub(ratio) {
                    if dip > worst {
                        worst = dip;
                    }
                }
            }
        }
    }
    worst
}

/// Return the longest peak-to-recovery span of the equity curve.
#[must_use]
pub fn longest_drawdown(points: &[EquityPoint]) -> DrawdownSpan {
    if validate_points(points).is_err() || points.len() < 2 {
        return DrawdownSpan {
            duration_ns: 0,
            recovered: true,
        };
    }
    let mut peak = points[0].equity;
    let mut peak_ts = points[0].timestamp_ns;
    let mut best_closed = 0i64;
    let mut open_peak_ts: Option<i64> = None;
    for point in &points[1..] {
        if point.equity >= peak {
            if let Some(opened) = open_peak_ts.take() {
                best_closed = best_closed.max(point.timestamp_ns - opened);
            }
            peak = point.equity;
            peak_ts = point.timestamp_ns;
        } else if open_peak_ts.is_none() {
            open_peak_ts = Some(peak_ts);
        }
    }
    if let Some(opened) = open_peak_ts {
        let open_span = points[points.len() - 1].timestamp_ns - opened;
        if open_span > best_closed {
            return DrawdownSpan {
                duration_ns: open_span,
                recovered: false,
            };
        }
    }
    DrawdownSpan {
        duration_ns: best_closed,
        recovered: true,
    }
}

/// Return every equity-curve metric for one curve at once.
#[must_use]
pub fn equity_curve_metrics(points: &[EquityPoint]) -> EquityMetrics {
    EquityMetrics {
        total_return: total_return(points),
        cagr: cagr(points),
        max_drawdown: max_drawdown(points),
        drawdown: longest_drawdown(points),
    }
}

/// Aggregate realized trade PnL values.
#[must_use]
pub fn trade_stats(trades: &[TradeRow]) -> TradeStats {
    let mut total = Decimal18::ZERO;
    let mut gross_profit = Decimal18::ZERO;
    let mut gross_loss = Decimal18::ZERO;
    let mut wins: Vec<Decimal18> = Vec::new();
    let mut losses: Vec<Decimal18> = Vec::new();
    for row in trades {
        let pnl = row.realized_pnl;
        total = total.checked_add(pnl).unwrap_or(total);
        if pnl.is_positive() {
            wins.push(pnl);
            gross_profit = gross_profit.checked_add(pnl).unwrap_or(gross_profit);
        } else if pnl.is_negative() {
            losses.push(pnl);
            gross_loss = gross_loss.checked_sub(pnl).unwrap_or(gross_loss);
        }
    }
    let win_count = wins.len();
    let loss_count = losses.len();
    let count = trades.len();
    let average_win = mean(&wins);
    let average_loss = mean(&losses);
    TradeStats {
        trade_count: count,
        win_count,
        loss_count,
        hit_rate: if count == 0 {
            None
        } else {
            Decimal18::from_i64(win_count as i64)
                .checked_div(Decimal18::from_i64(count as i64))
                .ok()
        },
        total_pnl: total,
        gross_profit,
        gross_loss,
        profit_factor: if gross_loss.is_positive() {
            gross_profit.checked_div(gross_loss).ok()
        } else {
            None
        },
        average_win,
        average_loss,
        largest_win: wins.iter().copied().max(),
        largest_loss: losses.iter().copied().min(),
    }
}

fn mean(values: &[Decimal18]) -> Option<Decimal18> {
    if values.is_empty() {
        return None;
    }
    let mut total = Decimal18::ZERO;
    for value in values {
        total = total.checked_add(*value).unwrap_or(total);
    }
    total
        .checked_div(Decimal18::from_i64(values.len() as i64))
        .ok()
}

/// Return the annualized Sharpe ratio, or `None` if undefined.
#[must_use]
pub fn sharpe(returns: &[Decimal18], periods_per_year: i64) -> Option<f64> {
    if returns.len() < 2 || periods_per_year <= 0 {
        return None;
    }
    let count = Decimal18::from_i64(returns.len() as i64);
    let mut sum = Decimal18::ZERO;
    for value in returns {
        sum = sum.checked_add(*value).unwrap_or(sum);
    }
    let mean = sum.checked_div(count).ok()?;
    let mut variance = Decimal18::ZERO;
    for value in returns {
        let deviation = value.checked_sub(mean).ok()?;
        let square = deviation.checked_mul(deviation).ok()?;
        variance = variance.checked_add(square).unwrap_or(variance);
    }
    variance = variance.checked_div(count).ok()?;
    if variance.is_zero() {
        return None;
    }
    let ratio = mean.to_f64() / variance.to_f64().sqrt();
    Some(ratio * (periods_per_year as f64).sqrt())
}

/// Return the annualized Sortino ratio, or `None` if undefined.
#[must_use]
pub fn sortino(returns: &[Decimal18], periods_per_year: i64, target: Decimal18) -> Option<f64> {
    if returns.is_empty() || periods_per_year <= 0 {
        return None;
    }
    let shortfalls: Vec<Decimal18> = returns
        .iter()
        .filter(|value| **value < target)
        .map(|value| value.checked_sub(target).unwrap_or(Decimal18::ZERO))
        .collect();
    if shortfalls.is_empty() {
        return None;
    }
    let count = Decimal18::from_i64(returns.len() as i64);
    let mut sum = Decimal18::ZERO;
    for value in returns {
        sum = sum.checked_add(*value).unwrap_or(sum);
    }
    let mean = sum.checked_div(count).ok()?;
    let mut variance = Decimal18::ZERO;
    for shortfall in &shortfalls {
        let square = shortfall.checked_mul(*shortfall).ok()?;
        variance = variance.checked_add(square).unwrap_or(variance);
    }
    variance = variance.checked_div(count).ok()?;
    if variance.is_zero() {
        return None;
    }
    let ratio = mean.to_f64() / variance.to_f64().sqrt();
    Some(ratio * (periods_per_year as f64).sqrt())
}

/// Aggregate fill latencies, ignoring `None` rows.
#[must_use]
pub fn latency_stats(fills: &[FillRow]) -> LatencyStats {
    let samples: Vec<i64> = fills.iter().filter_map(|fill| fill.latency_ns).collect();
    if samples.is_empty() {
        return LatencyStats {
            count: 0,
            mean_ns: None,
            max_ns: None,
        };
    }
    let sum: i64 = samples.iter().sum();
    LatencyStats {
        count: samples.len(),
        mean_ns: Decimal18::from_i64(sum)
            .checked_div(Decimal18::from_i64(samples.len() as i64))
            .ok(),
        max_ns: samples.iter().copied().max(),
    }
}

/// Count order outcomes by exact lowercase classification.
#[must_use]
pub fn order_outcome_counts(orders: &[OrderRow]) -> OrderOutcomeCounts {
    let mut counts = OrderOutcomeCounts {
        total: orders.len(),
        filled: 0,
        rejected: 0,
        cancelled: 0,
        other: 0,
    };
    for order in orders {
        match order.outcome.as_str() {
            "filled" => counts.filled += 1,
            "rejected" => counts.rejected += 1,
            "cancelled" => counts.cancelled += 1,
            _ => counts.other += 1,
        }
    }
    counts
}

/// Aggregate fees, slippage, latency, and outcomes over the ledgers.
#[must_use]
pub fn execution_stats(fills: &[FillRow], orders: &[OrderRow]) -> ExecutionStats {
    let mut fees_total = Decimal18::ZERO;
    let mut slippage_total = Decimal18::ZERO;
    for fill in fills {
        fees_total = fees_total.checked_add(fill.fee).unwrap_or(fees_total);
        slippage_total = slippage_total
            .checked_add(fill.slippage)
            .unwrap_or(slippage_total);
    }
    ExecutionStats {
        fill_count: fills.len(),
        fees_total,
        slippage_total,
        latency: latency_stats(fills),
        outcomes: order_outcome_counts(orders),
    }
}

/// Return the trade and execution aggregates for each strategy.
#[must_use]
pub fn per_strategy_attribution(
    fills: &[FillRow],
    orders: &[OrderRow],
    trades: &[TradeRow],
) -> BTreeMap<String, StrategyAttribution> {
    let mut grouped_fills: BTreeMap<&str, Vec<FillRow>> = BTreeMap::new();
    for fill in fills {
        grouped_fills
            .entry(fill.strategy_id.as_str())
            .or_default()
            .push(fill.clone());
    }
    let mut grouped_orders: BTreeMap<&str, Vec<OrderRow>> = BTreeMap::new();
    for order in orders {
        grouped_orders
            .entry(order.strategy_id.as_str())
            .or_default()
            .push(order.clone());
    }
    let mut grouped_trades: BTreeMap<&str, Vec<TradeRow>> = BTreeMap::new();
    for trade in trades {
        grouped_trades
            .entry(trade.strategy_id.as_str())
            .or_default()
            .push(trade.clone());
    }
    let mut strategies: Vec<&str> = grouped_fills
        .keys()
        .chain(grouped_orders.keys())
        .chain(grouped_trades.keys())
        .copied()
        .collect();
    strategies.sort_unstable();
    strategies.dedup();
    strategies
        .into_iter()
        .map(|strategy_id| {
            let empty_fills: &[FillRow] = &[];
            let empty_orders: &[OrderRow] = &[];
            let empty_trades: &[TradeRow] = &[];
            let attribution = StrategyAttribution {
                strategy_id: strategy_id.to_string(),
                trades: trade_stats(
                    grouped_trades
                        .get(strategy_id)
                        .map_or(empty_trades, Vec::as_slice),
                ),
                execution: execution_stats(
                    grouped_fills
                        .get(strategy_id)
                        .map_or(empty_fills, Vec::as_slice),
                    grouped_orders
                        .get(strategy_id)
                        .map_or(empty_orders, Vec::as_slice),
                ),
            };
            (strategy_id.to_string(), attribution)
        })
        .collect()
}

/// Parse a string-exact decimal for callers building metric rows.
#[must_use]
pub fn decimal(value: &str) -> Decimal18 {
    dec(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn point(timestamp_ns: i64, equity: &str) -> EquityPoint {
        EquityPoint {
            timestamp_ns,
            equity: dec(equity),
        }
    }

    #[test]
    fn total_return_and_drawdown() {
        let points = [point(0, "100"), point(10, "150"), point(20, "120")];
        assert_eq!(total_return(&points), Some(dec("0.2")));
        // Peak 150 to trough 120 => 1 - 120/150 = 0.2
        assert_eq!(max_drawdown(&points), dec("0.2"));
        assert_eq!(longest_drawdown(&points).duration_ns, 10);
    }

    #[test]
    fn single_point_curve_is_undefined() {
        let points = [point(0, "100")];
        assert_eq!(total_return(&points), None);
        assert_eq!(cagr(&points), None);
        assert_eq!(max_drawdown(&points), Decimal18::ZERO);
    }

    #[test]
    fn trade_stats_classify_wins_and_losses() {
        let trades = vec![
            TradeRow {
                trade_id: "a".to_string(),
                strategy_id: "s".to_string(),
                realized_pnl: dec("10"),
            },
            TradeRow {
                trade_id: "b".to_string(),
                strategy_id: "s".to_string(),
                realized_pnl: dec("-4"),
            },
            TradeRow {
                trade_id: "c".to_string(),
                strategy_id: "s".to_string(),
                realized_pnl: Decimal18::ZERO,
            },
        ];
        let stats = trade_stats(&trades);
        assert_eq!(stats.trade_count, 3);
        assert_eq!(stats.win_count, 1);
        assert_eq!(stats.loss_count, 1);
        assert_eq!(stats.gross_profit, dec("10"));
        assert_eq!(stats.gross_loss, dec("4"));
        assert_eq!(stats.profit_factor, Some(dec("2.5")));
        assert_eq!(stats.largest_win, Some(dec("10")));
        assert_eq!(stats.largest_loss, Some(dec("-4")));
    }

    #[test]
    fn outcome_counts_classify_by_exact_string() {
        let orders = vec![
            OrderRow {
                order_id: "1".to_string(),
                strategy_id: "s".to_string(),
                outcome: "filled".to_string(),
            },
            OrderRow {
                order_id: "2".to_string(),
                strategy_id: "s".to_string(),
                outcome: "FILLED".to_string(),
            },
        ];
        let counts = order_outcome_counts(&orders);
        assert_eq!(counts.filled, 1);
        assert_eq!(counts.other, 1);
    }

    #[test]
    fn per_strategy_attribution_unions_the_ledgers() {
        let fills = vec![FillRow {
            fill_id: "f".to_string(),
            order_id: "o".to_string(),
            strategy_id: "a".to_string(),
            quantity: dec("1"),
            price: dec("10"),
            fee: dec("0.5"),
            slippage: dec("0.25"),
            latency_ns: Some(1000),
        }];
        let orders = vec![OrderRow {
            order_id: "o".to_string(),
            strategy_id: "b".to_string(),
            outcome: "rejected".to_string(),
        }];
        let attribution = per_strategy_attribution(&fills, &orders, &[]);
        assert_eq!(attribution.len(), 2);
        assert_eq!(attribution["a"].execution.fees_total, dec("0.5"));
        assert_eq!(attribution["b"].execution.outcomes.rejected, 1);
        assert_eq!(attribution["b"].execution.fill_count, 0);
    }

    #[test]
    fn sharpe_is_none_without_variance() {
        assert_eq!(sharpe(&[dec("1"), dec("1")], 252), None);
        assert!(sharpe(&[dec("1"), dec("2"), dec("3")], 252).is_some());
    }
}
