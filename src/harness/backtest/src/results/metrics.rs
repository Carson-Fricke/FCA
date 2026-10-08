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
//! zero net loss for the profit factor.

use num::Decimal18;

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

/// One closed-trade slice: a FIFO lot slice matched against one closing fill.
///
/// Fees follow `fill_quantity_share.v1`: the entry fee is the slice's share of
/// the opening lot's remaining fee, and the exit fee is its share of the closing
/// fill's remaining fee, each rounded half-even at scale 18.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TradeRow {
    /// Trade identifier in exit order, `trade-0001` onward.
    pub trade_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// `long` when the opening fill bought, `short` when it sold.
    pub side: String,
    /// Matched quantity, positive.
    pub quantity: Decimal18,
    /// Sequence of the fill that opened the matched lot.
    pub entry_sequence: u64,
    /// Sequence of the fill that closed the slice.
    pub exit_sequence: u64,
    /// Epoch nanoseconds of the opening fill.
    pub entry_timestamp_ns: i64,
    /// Epoch nanoseconds of the closing fill.
    pub exit_timestamp_ns: i64,
    /// Entry price.
    pub entry_price: Decimal18,
    /// Exit price.
    pub exit_price: Decimal18,
    /// Price PnL before fees.
    pub gross_pnl: Decimal18,
    /// Allocated entry fee.
    pub entry_fee: Decimal18,
    /// Allocated exit fee.
    pub exit_fee: Decimal18,
    /// `entry_fee + exit_fee`.
    pub fees: Decimal18,
    /// `gross_pnl - fees`.
    pub net_pnl: Decimal18,
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

/// Aggregates over closed trades. Win, loss, hit rate, and profit factor classify
/// on net PnL; the gross fields bridge to fees.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TradeStats {
    /// Number of closed trade slices.
    pub trade_count: usize,
    /// Slices with positive net PnL.
    pub win_count: usize,
    /// Slices with negative net PnL.
    pub loss_count: usize,
    /// Slices with zero net PnL.
    pub zero_count: usize,
    /// Sum of gross PnL.
    pub gross_pnl: Decimal18,
    /// Sum of allocated fees over closed slices.
    pub fees: Decimal18,
    /// Sum of net PnL.
    pub net_pnl: Decimal18,
    /// Fees still attached to open lots at run end.
    pub open_fees: Decimal18,
    /// Sum of positive gross PnL.
    pub gross_profit: Decimal18,
    /// Magnitude of negative gross PnL.
    pub gross_loss: Decimal18,
    /// Sum of positive net PnL.
    pub net_profit: Decimal18,
    /// Magnitude of negative net PnL.
    pub net_loss: Decimal18,
    /// `net_profit / net_loss`, if defined.
    pub profit_factor: Option<Decimal18>,
    /// Why `profit_factor` is null: `no_closed_trades` or `no_net_losses`.
    pub profit_factor_reason: Option<&'static str>,
    /// Share of slices with positive net PnL, if there are trades.
    pub hit_rate: Option<Decimal18>,
    /// Why `hit_rate` is null: `no_closed_trades`.
    pub hit_rate_reason: Option<&'static str>,
    /// Mean positive net PnL, if any.
    pub average_net_win: Option<Decimal18>,
    /// Mean negative net PnL (signed), if any.
    pub average_net_loss: Option<Decimal18>,
    /// Largest positive net PnL, if any.
    pub largest_net_win: Option<Decimal18>,
    /// Most negative net PnL (signed), if any.
    pub largest_net_loss: Option<Decimal18>,
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

/// Aggregate closed trade slices and the fees still attached to open lots.
#[must_use]
pub fn trade_stats(trades: &[TradeRow], open_fees: Decimal18) -> TradeStats {
    let zero = Decimal18::ZERO;
    let mut stats = TradeStats {
        trade_count: trades.len(),
        win_count: 0,
        loss_count: 0,
        zero_count: 0,
        gross_pnl: zero,
        fees: zero,
        net_pnl: zero,
        open_fees,
        gross_profit: zero,
        gross_loss: zero,
        net_profit: zero,
        net_loss: zero,
        profit_factor: None,
        profit_factor_reason: None,
        hit_rate: None,
        hit_rate_reason: None,
        average_net_win: None,
        average_net_loss: None,
        largest_net_win: None,
        largest_net_loss: None,
    };
    let mut wins: Vec<Decimal18> = Vec::new();
    let mut losses: Vec<Decimal18> = Vec::new();
    for row in trades {
        stats.gross_pnl = stats
            .gross_pnl
            .checked_add(row.gross_pnl)
            .unwrap_or(stats.gross_pnl);
        stats.fees = stats.fees.checked_add(row.fees).unwrap_or(stats.fees);
        stats.net_pnl = stats
            .net_pnl
            .checked_add(row.net_pnl)
            .unwrap_or(stats.net_pnl);
        if row.gross_pnl.is_positive() {
            stats.gross_profit = stats
                .gross_profit
                .checked_add(row.gross_pnl)
                .unwrap_or(stats.gross_profit);
        } else if row.gross_pnl.is_negative() {
            stats.gross_loss = stats
                .gross_loss
                .checked_sub(row.gross_pnl)
                .unwrap_or(stats.gross_loss);
        }
        if row.net_pnl.is_positive() {
            stats.net_profit = stats
                .net_profit
                .checked_add(row.net_pnl)
                .unwrap_or(stats.net_profit);
            wins.push(row.net_pnl);
        } else if row.net_pnl.is_negative() {
            stats.net_loss = stats
                .net_loss
                .checked_sub(row.net_pnl)
                .unwrap_or(stats.net_loss);
            losses.push(row.net_pnl);
        } else {
            stats.zero_count += 1;
        }
    }
    stats.win_count = wins.len();
    stats.loss_count = losses.len();
    if trades.is_empty() {
        stats.hit_rate_reason = Some("no_closed_trades");
        stats.profit_factor_reason = Some("no_closed_trades");
    } else {
        stats.hit_rate = Decimal18::from_i64(stats.win_count as i64)
            .checked_div(Decimal18::from_i64(trades.len() as i64))
            .ok();
        if stats.net_loss.is_zero() {
            stats.profit_factor_reason = Some("no_net_losses");
        } else {
            stats.profit_factor = stats.net_profit.checked_div(stats.net_loss).ok();
        }
    }
    stats.average_net_win = mean(&wins);
    stats.average_net_loss = mean(&losses);
    stats.largest_net_win = wins.iter().copied().max();
    stats.largest_net_loss = losses.iter().copied().min();
    stats
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

/// Return the Sharpe ratio over per-sample returns, annualized by
/// `sqrt(periods_per_year)`, using the sample (n - 1) standard deviation.
///
/// Returns `None` when there are fewer than two returns or zero volatility.
#[must_use]
pub fn sharpe(returns: &[Decimal18], periods_per_year: i64) -> Option<f64> {
    if returns.len() < 2 || periods_per_year <= 0 {
        return None;
    }
    let count = Decimal18::from_i64(returns.len() as i64);
    let degrees = Decimal18::from_i64((returns.len() - 1) as i64);
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
    variance = variance.checked_div(degrees).ok()?;
    if variance.is_zero() {
        return None;
    }
    let ratio = mean.to_f64() / variance.to_f64().sqrt();
    Some(ratio * (periods_per_year as f64).sqrt())
}

/// Trading samples per year assumed when annualizing equity-curve Sharpe.
pub const SHARPE_PERIODS_PER_YEAR: i64 = 252;

/// Equity-curve Sharpe with the reason it is null.
///
/// Per-sample returns are `equity[i] / equity[i - 1] - 1` for every non-zero
/// predecessor. Null reasons are `fewer_than_three_samples`,
/// `fewer_than_two_returns`, and `zero_volatility`.
#[must_use]
pub fn equity_sharpe(points: &[EquityPoint]) -> (Option<f64>, Option<&'static str>) {
    if points.len() < 3 {
        return (None, Some("fewer_than_three_samples"));
    }
    let mut returns: Vec<Decimal18> = Vec::new();
    for pair in points.windows(2) {
        if pair[0].equity.is_zero() {
            continue;
        }
        if let Ok(ratio) = pair[1].equity.checked_div(pair[0].equity) {
            if let Ok(value) = ratio.checked_sub(Decimal18::ONE) {
                returns.push(value);
            }
        }
    }
    if returns.len() < 2 {
        return (None, Some("fewer_than_two_returns"));
    }
    match sharpe(&returns, SHARPE_PERIODS_PER_YEAR) {
        Some(value) => (Some(value), None),
        None => (None, Some("zero_volatility")),
    }
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

/// Parse a string-exact decimal for callers building metric rows.
#[must_use]
pub fn decimal(value: &str) -> Decimal18 {
    dec(value)
}
