//! Pure, deterministic metrics for backtest results.
//!
//! The canonical financial statistics implementation. Every function maps
//! plain data to plain data: no I/O, wall clock, randomness, or network. Money
//! and ratio arithmetic uses the scale-18 fixed-point kernel. Only the two
//! statistics that need a square root or a fractional exponent (the sample
//! standard deviation behind the per-sample Sharpe and the CAGR exponent)
//! leave that kernel, at their final step.
//!
//! Undefined values are explicit. A metric that cannot be defined is
//! `Err(reason)` with a closed reason name (see [`Outcome`]); the job publishes
//! it as JSON null beside a `*_reason` key, never as zero. Exact arithmetic
//! that leaves the decimal128(38,18) bound is a [`MetricsError`]. Ledger totals
//! fail the job rather than publish partial sums; independent equity statistics
//! publish an `arithmetic_overflow` reason while retaining valid ledger totals.
//!
//! Equity points must be ordered by non-decreasing timestamp. The job builds
//! them in dispatch order and checks that order before calling these functions.

use std::fmt;

use num::Decimal18;

/// Nanoseconds per day.
pub const NANOS_PER_DAY: i64 = 86_400_000_000_000;
/// Nanoseconds per 365-day year.
pub const NANOS_PER_YEAR_365: i64 = 365 * NANOS_PER_DAY;

/// The outcome of one metric: its value, or the closed reason it is undefined.
pub type Outcome<T> = Result<T, &'static str>;

/// Exact decimal arithmetic left the decimal128(38,18) bound.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MetricsError {
    /// The metric whose arithmetic overflowed.
    pub metric: &'static str,
}

impl fmt::Display for MetricsError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} overflows the decimal128(38,18) bound",
            self.metric
        )
    }
}

impl std::error::Error for MetricsError {}

const fn overflow(metric: &'static str) -> MetricsError {
    MetricsError { metric }
}

fn add(left: Decimal18, right: Decimal18, metric: &'static str) -> Result<Decimal18, MetricsError> {
    left.checked_add(right).map_err(|_| overflow(metric))
}

fn sub(left: Decimal18, right: Decimal18, metric: &'static str) -> Result<Decimal18, MetricsError> {
    left.checked_sub(right).map_err(|_| overflow(metric))
}

fn div(left: Decimal18, right: Decimal18, metric: &'static str) -> Result<Decimal18, MetricsError> {
    left.checked_div(right).map_err(|_| overflow(metric))
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
    /// Trade identifier in exit order, starting at 1.
    pub trade_id: u64,
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

/// Aggregate metrics over one equity curve. Each field is either its value or
/// the closed reason it is undefined.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EquityMetrics {
    /// `last / first - 1`.
    pub total_return: Outcome<Decimal18>,
    /// 365-day CAGR as a fraction (`0.5` is 50 percent per year).
    pub cagr: Outcome<f64>,
    /// Largest `1 - trough / peak`.
    pub max_drawdown: Outcome<Decimal18>,
    /// Longest peak-to-recovery span. Undefined exactly when `max_drawdown` is,
    /// with the same reason.
    pub drawdown: Outcome<DrawdownSpan>,
    /// Mean over the sample standard deviation of per-sample returns; not
    /// annualized.
    pub sharpe_per_sample: Outcome<f64>,
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
    /// Latency aggregates.
    pub latency: LatencyStats,
    /// Order outcome counts.
    pub outcomes: OrderOutcomeCounts,
}

fn has_negative(points: &[EquityPoint]) -> bool {
    points.iter().any(|point| point.equity.is_negative())
}

/// The shared undefined-value checks for the first-to-last ratio metrics.
fn ratio_reason(points: &[EquityPoint]) -> Option<&'static str> {
    if points.len() < 2 {
        return Some("fewer_than_two_samples");
    }
    if !points[0].equity.is_positive() {
        return Some("nonpositive_initial_equity");
    }
    if has_negative(points) {
        return Some("negative_equity");
    }
    None
}

/// Return `last / first - 1`, or the reason it is undefined.
///
/// # Errors
///
/// Returns [`MetricsError`] when the exact ratio leaves the decimal bound.
pub fn total_return(points: &[EquityPoint]) -> Result<Outcome<Decimal18>, MetricsError> {
    if let Some(reason) = ratio_reason(points) {
        return Ok(Err(reason));
    }
    let first = points[0].equity;
    let last = points[points.len() - 1].equity;
    let ratio = div(last, first, "total_return")?;
    Ok(Ok(sub(ratio, Decimal18::ONE, "total_return")?))
}

/// Return the 365-day-year CAGR as a fraction, or the reason it is undefined.
///
/// The value is `(last / first)^(365 days / span) - 1`.
///
/// # Errors
///
/// Arithmetic that cannot be represented, including nonfinite annualization,
/// yields the explicit `arithmetic_overflow` undefined reason.
pub fn cagr(points: &[EquityPoint]) -> Result<Outcome<f64>, MetricsError> {
    if let Some(reason) = ratio_reason(points) {
        return Ok(Err(reason));
    }
    let first = points[0];
    let last = points[points.len() - 1];
    let Some(span) = last.timestamp_ns.checked_sub(first.timestamp_ns) else {
        return Ok(Err("arithmetic_overflow"));
    };
    if span <= 0 {
        return Ok(Err("nonpositive_span"));
    }
    let Ok(growth) = div(last.equity, first.equity, "cagr") else {
        return Ok(Err("arithmetic_overflow"));
    };
    let growth = growth.to_f64();
    let years = span as f64 / NANOS_PER_YEAR_365 as f64;
    let value = growth.powf(1.0 / years) - 1.0;
    if value.is_finite() {
        Ok(Ok(value))
    } else {
        Ok(Err("arithmetic_overflow"))
    }
}

/// Return the largest `1 - trough / peak`, or `negative_equity` when any sample
/// is negative.
///
/// # Errors
///
/// Returns [`MetricsError`] when an exact drawdown ratio leaves the decimal
/// bound.
pub fn max_drawdown(points: &[EquityPoint]) -> Result<Outcome<Decimal18>, MetricsError> {
    if has_negative(points) {
        return Ok(Err("negative_equity"));
    }
    let mut peak = points.first().map_or(Decimal18::ZERO, |point| point.equity);
    let mut worst = Decimal18::ZERO;
    for point in points {
        if point.equity > peak {
            peak = point.equity;
        } else if peak.is_positive() && point.equity < peak {
            let ratio = div(point.equity, peak, "max_drawdown")?;
            let dip = sub(Decimal18::ONE, ratio, "max_drawdown")?;
            if dip > worst {
                worst = dip;
            }
        }
    }
    Ok(Ok(worst))
}

fn span_ns(from: i64, to: i64) -> Result<i64, MetricsError> {
    to.checked_sub(from)
        .ok_or_else(|| overflow("drawdown_duration_ns"))
}

/// Return the longest peak-to-recovery span, or `negative_equity` when any
/// sample is negative. A curve with fewer than two samples has no drawdown and
/// reports a zero span that recovered.
///
/// # Errors
///
/// Returns [`MetricsError`] when a span does not fit in an `i64`.
pub fn longest_drawdown(points: &[EquityPoint]) -> Result<Outcome<DrawdownSpan>, MetricsError> {
    if has_negative(points) {
        return Ok(Err("negative_equity"));
    }
    let Some(first) = points.first() else {
        return Ok(Ok(DrawdownSpan {
            duration_ns: 0,
            recovered: true,
        }));
    };
    let mut peak = first.equity;
    let mut peak_ts = first.timestamp_ns;
    let mut best_closed = 0i64;
    let mut open_peak_ts: Option<i64> = None;
    for point in &points[1..] {
        if point.equity >= peak {
            if let Some(opened) = open_peak_ts.take() {
                best_closed = best_closed.max(span_ns(opened, point.timestamp_ns)?);
            }
            peak = point.equity;
            peak_ts = point.timestamp_ns;
        } else if open_peak_ts.is_none() {
            open_peak_ts = Some(peak_ts);
        }
    }
    if let (Some(opened), Some(last)) = (open_peak_ts, points.last()) {
        let open_span = span_ns(opened, last.timestamp_ns)?;
        if open_span > best_closed {
            return Ok(Ok(DrawdownSpan {
                duration_ns: open_span,
                recovered: false,
            }));
        }
    }
    Ok(Ok(DrawdownSpan {
        duration_ns: best_closed,
        recovered: true,
    }))
}

/// Return the per-sample Sharpe: the mean of the per-sample returns divided by
/// their sample (n - 1) standard deviation. It is not annualized.
///
/// The per-sample returns are `r_i = (E_i - E_{i-1}) / E_{i-1}` over consecutive
/// samples. The reasons are checked in this order:
///
/// - `fewer_than_three_returns`: fewer than three returns exist.
/// - `negative_equity`: a sample is negative, or a sample before the last is
///   zero, which leaves its return without a denominator. The spec gives no
///   separate reason for a zero predecessor, so it shares this one.
/// - `zero_volatility`: every return is equal, so the deviation is zero.
///
/// # Errors
///
/// Returns [`MetricsError`] when exact arithmetic leaves the decimal bound.
pub fn sharpe_per_sample(points: &[EquityPoint]) -> Result<Outcome<f64>, MetricsError> {
    let return_count = points.len().saturating_sub(1);
    if return_count < 3 {
        return Ok(Err("fewer_than_three_returns"));
    }
    if has_negative(points) {
        return Ok(Err("negative_equity"));
    }
    let mut returns: Vec<Decimal18> = Vec::with_capacity(return_count);
    for pair in points.windows(2) {
        let previous = pair[0].equity;
        if previous.is_zero() {
            return Ok(Err("negative_equity"));
        }
        let ratio = div(pair[1].equity, previous, "sharpe_per_sample")?;
        returns.push(sub(ratio, Decimal18::ONE, "sharpe_per_sample")?);
    }
    let mut sum = Decimal18::ZERO;
    for value in &returns {
        sum = add(sum, *value, "sharpe_per_sample")?;
    }
    let mean = div(
        sum,
        Decimal18::from_i64(returns.len() as i64),
        "sharpe_per_sample",
    )?;
    // Deviations are exact decimal differences. Squares are taken in f64 so that
    // unequal returns cannot round to zero volatility at scale 18.
    let mut squares = 0.0f64;
    for value in &returns {
        let deviation = sub(*value, mean, "sharpe_per_sample")?.to_f64();
        squares += deviation * deviation;
    }
    if squares <= 0.0 {
        return Ok(Err("zero_volatility"));
    }
    let variance = squares / (returns.len() - 1) as f64;
    let value = mean.to_f64() / variance.sqrt();
    if value.is_finite() {
        Ok(Ok(value))
    } else {
        Err(overflow("sharpe_per_sample"))
    }
}

/// Return every equity-curve metric, preserving each undefined statistic.
/// Numeric-statistic overflow is evidence about that statistic, not a failure
/// of an otherwise exact cash/fill ledger.
pub fn equity_curve_metrics(points: &[EquityPoint]) -> EquityMetrics {
    EquityMetrics {
        total_return: total_return(points).unwrap_or(Err("arithmetic_overflow")),
        cagr: cagr(points).unwrap_or(Err("arithmetic_overflow")),
        max_drawdown: max_drawdown(points).unwrap_or(Err("arithmetic_overflow")),
        drawdown: longest_drawdown(points).unwrap_or(Err("arithmetic_overflow")),
        sharpe_per_sample: sharpe_per_sample(points).unwrap_or(Err("arithmetic_overflow")),
    }
}

/// Mean of a slice of decimals, or `None` when it is empty.
fn mean(values: &[Decimal18], metric: &'static str) -> Result<Option<Decimal18>, MetricsError> {
    if values.is_empty() {
        return Ok(None);
    }
    let mut total = Decimal18::ZERO;
    for value in values {
        total = add(total, *value, metric)?;
    }
    div(total, Decimal18::from_i64(values.len() as i64), metric).map(Some)
}

/// Aggregate closed trade slices and the fees still attached to open lots.
///
/// # Errors
///
/// Returns [`MetricsError`] when a sum or ratio leaves the decimal bound.
pub fn trade_stats(trades: &[TradeRow], open_fees: Decimal18) -> Result<TradeStats, MetricsError> {
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
        stats.gross_pnl = add(stats.gross_pnl, row.gross_pnl, "gross_pnl")?;
        stats.fees = add(stats.fees, row.fees, "fees")?;
        stats.net_pnl = add(stats.net_pnl, row.net_pnl, "net_pnl")?;
        if row.gross_pnl.is_positive() {
            stats.gross_profit = add(stats.gross_profit, row.gross_pnl, "gross_profit")?;
        } else if row.gross_pnl.is_negative() {
            stats.gross_loss = sub(stats.gross_loss, row.gross_pnl, "gross_loss")?;
        }
        if row.net_pnl.is_positive() {
            stats.net_profit = add(stats.net_profit, row.net_pnl, "net_profit")?;
            wins.push(row.net_pnl);
        } else if row.net_pnl.is_negative() {
            stats.net_loss = sub(stats.net_loss, row.net_pnl, "net_loss")?;
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
        stats.hit_rate = Some(div(
            Decimal18::from_i64(stats.win_count as i64),
            Decimal18::from_i64(trades.len() as i64),
            "hit_rate",
        )?);
        if stats.net_loss.is_zero() {
            stats.profit_factor_reason = Some("no_net_losses");
        } else {
            stats.profit_factor = Some(div(stats.net_profit, stats.net_loss, "profit_factor")?);
        }
    }
    stats.average_net_win = mean(&wins, "average_net_win")?;
    stats.average_net_loss = mean(&losses, "average_net_loss")?;
    stats.largest_net_win = wins.iter().copied().max();
    stats.largest_net_loss = losses.iter().copied().min();
    Ok(stats)
}

/// Aggregate latency, ignoring `None` rows.
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

/// Aggregate fees, latency, and outcomes over the ledgers.
///
/// # Errors
///
/// Returns [`MetricsError`] when the fee total leaves the decimal bound.
pub fn execution_stats(
    fills: &[FillRow],
    orders: &[OrderRow],
) -> Result<ExecutionStats, MetricsError> {
    let mut fees_total = Decimal18::ZERO;
    for fill in fills {
        fees_total = add(fees_total, fill.fee, "fees_total")?;
    }
    Ok(ExecutionStats {
        fill_count: fills.len(),
        fees_total,
        latency: latency_stats(fills),
        outcomes: order_outcome_counts(orders),
    })
}
