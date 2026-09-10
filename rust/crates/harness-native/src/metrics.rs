//! Metrics kernels ported from `src/harness/native/metrics/*.zig`.
//!
//! Conventions (mirroring `src/harness/results/metrics.py`): money and ratios
//! are exact fixed point on the scale-18 `i128` kernel; only the final
//! annualization steps leave fixed point for `f64`; genuinely undefined metrics
//! are `None` rather than a sentinel. The documented pinned-scale divergence:
//! Python rounds Decimal arithmetic to 28 significant digits, the native kernel
//! rounds half-even at scale 18.

use crate::decimal::{self, DecimalError};
use crate::wide::U256;

/// A checked failure from a metrics kernel.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MetricsError {
    /// Fixed-point overflow or a non-representable value.
    OutOfRange,
    /// Division by zero or otherwise invalid decimal operation.
    InvalidDecimal,
    /// The input ledger was invalid.
    InvalidInput,
}

impl core::fmt::Display for MetricsError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::OutOfRange => "fixed-point result is outside decimal128(38, 18)",
            Self::InvalidDecimal => "invalid decimal operation",
            Self::InvalidInput => "invalid metrics input",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for MetricsError {}

fn decimal_error(error: DecimalError) -> MetricsError {
    match error {
        DecimalError::OutOfRange => MetricsError::OutOfRange,
        DecimalError::InvalidDecimal => MetricsError::InvalidDecimal,
    }
}

/// One unscaled unit.
pub const ONE: i128 = decimal::ONE;

/// Zero at the pinned scale.
pub const ZERO: i128 = 0;

/// Nanoseconds in a UTC day.
pub const NANOS_PER_DAY: i64 = 86_400_000_000_000;

/// Nanoseconds in a 365-day year.
pub const NANOS_PER_YEAR_365: i64 = 365 * NANOS_PER_DAY;

/// Epoch-nanosecond equity-curve point.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EquityPoint {
    /// Epoch nanoseconds; points arrive in ascending order.
    pub timestamp_ns: i64,
    /// Equity at the pinned scale.
    pub equity: i128,
}

/// One realized-trade row.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TradeRow {
    /// Signed realized PnL at the pinned scale.
    pub realized_pnl: i128,
}

/// One fill row.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FillRow {
    /// Fill quantity.
    pub quantity: i128,
    /// Fill price.
    pub price: i128,
    /// Fill fee.
    pub fee: i128,
    /// Signed per-fill cost: `quantity * (fill_price - reference_price)`.
    pub slippage: i128,
    /// Optional fill latency in nanoseconds.
    pub latency_ns: Option<i64>,
}

/// One order outcome row.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OrderRow<'a> {
    /// Exact lowercase outcome.
    pub outcome: &'a str,
}

/// A peak-to-recovery drawdown span.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DrawdownSpan {
    /// Span duration in nanoseconds.
    pub duration_ns: i64,
    /// Whether the span recovered.
    pub recovered: bool,
}

/// Equity-curve metrics.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EquityMetrics {
    /// Total return, or `None` when undefined.
    pub total_return: Option<i128>,
    /// 365-day CAGR, or `None` when undefined.
    pub cagr: Option<f64>,
    /// Maximum drawdown.
    pub max_drawdown: i128,
    /// Longest drawdown span.
    pub drawdown: DrawdownSpan,
}

/// Realized-trade aggregates.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TradeStats {
    /// Number of trades.
    pub trade_count: usize,
    /// Number of winners.
    pub win_count: usize,
    /// Number of losers.
    pub loss_count: usize,
    /// Hit rate, or `None` when empty.
    pub hit_rate: Option<i128>,
    /// Total PnL.
    pub total_pnl: i128,
    /// Gross profit.
    pub gross_profit: i128,
    /// Gross loss magnitude.
    pub gross_loss: i128,
    /// Profit factor, or `None` without losses.
    pub profit_factor: Option<i128>,
    /// Average win.
    pub average_win: Option<i128>,
    /// Average loss.
    pub average_loss: Option<i128>,
    /// Largest win.
    pub largest_win: Option<i128>,
    /// Largest loss.
    pub largest_loss: Option<i128>,
}

/// Latency aggregates.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LatencyStats {
    /// Number of timed fills.
    pub count: usize,
    /// Mean latency, or `None` when empty.
    pub mean_ns: Option<i128>,
    /// Maximum latency, or `None` when empty.
    pub max_ns: Option<i64>,
}

/// Order outcome counts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OrderOutcomeCounts {
    /// Total orders.
    pub total: usize,
    /// Filled orders.
    pub filled: usize,
    /// Rejected orders.
    pub rejected: usize,
    /// Cancelled orders.
    pub cancelled: usize,
    /// Other orders.
    pub other: usize,
}

/// Combined execution aggregates.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExecutionStats {
    /// Number of fills.
    pub fill_count: usize,
    /// Total fees.
    pub fees_total: i128,
    /// Total slippage.
    pub slippage_total: i128,
    /// Latency aggregates.
    pub latency: LatencyStats,
    /// Outcome counts.
    pub outcomes: OrderOutcomeCounts,
}

/// Per-strategy attribution row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StrategyAttribution {
    /// Strategy identifier.
    pub strategy_id: String,
    /// Trade aggregates.
    pub trades: TradeStats,
    /// Execution aggregates.
    pub execution: ExecutionStats,
}

/// A strategy-tagged fill row.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AnnotatedFill<'a> {
    /// Strategy identifier.
    pub strategy_id: &'a str,
    /// Fill row.
    pub fill: FillRow,
}

/// A strategy-tagged order row.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AnnotatedOrder<'a> {
    /// Strategy identifier.
    pub strategy_id: &'a str,
    /// Order row.
    pub order: OrderRow<'a>,
}

/// A strategy-tagged trade row.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AnnotatedTrade<'a> {
    /// Strategy identifier.
    pub strategy_id: &'a str,
    /// Trade row.
    pub trade: TradeRow,
}

/// Convert a scale-18 value to `f64`.
pub fn unscaled_to_f64(value: i128) -> f64 {
    value as f64 / ONE as f64
}

/// Parse a scale-18 value from text (at most 18 fractional digits).
pub fn dec_text(text: &str) -> i128 {
    let negative = text.starts_with('-');
    let body = text.strip_prefix('-').unwrap_or(text);
    let mut integer: i128 = 0;
    let mut fraction: i128 = 0;
    let mut fraction_digits = 0usize;
    let mut seen_dot = false;
    for byte in body.bytes() {
        if byte == b'.' {
            seen_dot = true;
        } else if !seen_dot {
            integer = integer * 10 + i128::from(byte - b'0');
        } else if fraction_digits < 18 {
            fraction = fraction * 10 + i128::from(byte - b'0');
            fraction_digits += 1;
        }
    }
    let mut scaled = integer * ONE;
    while fraction_digits < 18 {
        fraction *= 10;
        fraction_digits += 1;
    }
    scaled += fraction;
    if negative {
        -scaled
    } else {
        scaled
    }
}

fn validate_points(points: &[EquityPoint]) -> Result<(), MetricsError> {
    for (index, point) in points.iter().enumerate() {
        if point.equity < ZERO {
            return Err(MetricsError::InvalidInput);
        }
        if index > 0 && point.timestamp_ns < points[index - 1].timestamp_ns {
            return Err(MetricsError::InvalidInput);
        }
    }
    Ok(())
}

/// `last/first - 1` over the curve; `None` for fewer than two points or a zero
/// first equity.
pub fn total_return(points: &[EquityPoint]) -> Result<Option<i128>, MetricsError> {
    validate_points(points)?;
    if points.len() < 2 || points[0].equity == ZERO {
        return Ok(None);
    }
    let ratio = decimal::div(points[points.len() - 1].equity, points[0].equity)
        .map_err(decimal_error)?;
    Ok(Some(decimal::sub(ratio, ONE).map_err(decimal_error)?))
}

/// 365-day-year CAGR; `None` when undefined.
pub fn cagr(points: &[EquityPoint]) -> Result<Option<f64>, MetricsError> {
    validate_points(points)?;
    if points.len() < 2 {
        return Ok(None);
    }
    let start = points[0];
    let end = points[points.len() - 1];
    let span = end.timestamp_ns - start.timestamp_ns;
    if span <= 0 || start.equity <= ZERO {
        return Ok(None);
    }
    let growth = decimal::div(end.equity, start.equity).map_err(decimal_error)?;
    if growth <= ZERO {
        return Ok(None);
    }
    let years = decimal::div(
        i128::from(span) * ONE,
        i128::from(NANOS_PER_YEAR_365) * ONE,
    )
    .map_err(decimal_error)?;
    let exponent = decimal::div(ONE, years).map_err(decimal_error)?;
    Ok(Some(
        unscaled_to_f64(growth).powf(unscaled_to_f64(exponent)),
    ))
}

/// Largest `1 - trough/peak` against the running peak.
pub fn max_drawdown(points: &[EquityPoint]) -> Result<i128, MetricsError> {
    validate_points(points)?;
    let mut peak = points.first().map_or(ZERO, |point| point.equity);
    let mut worst = ZERO;
    for point in points {
        if point.equity > peak {
            peak = point.equity;
        } else if peak > ZERO && point.equity < peak {
            let drawdown = decimal::sub(
                ONE,
                decimal::div(point.equity, peak).map_err(decimal_error)?,
            )
            .map_err(decimal_error)?;
            if drawdown > worst {
                worst = drawdown;
            }
        }
    }
    Ok(worst)
}

/// Longest peak-to-recovery span; ties prefer the recovered span.
pub fn longest_drawdown(points: &[EquityPoint]) -> Result<DrawdownSpan, MetricsError> {
    validate_points(points)?;
    if points.len() < 2 {
        return Ok(DrawdownSpan {
            duration_ns: 0,
            recovered: true,
        });
    }
    let mut peak = points[0].equity;
    let mut peak_ts = points[0].timestamp_ns;
    let mut best_closed: i64 = 0;
    let mut open_peak_ts: Option<i64> = None;
    for point in &points[1..] {
        if point.equity >= peak {
            if let Some(open_ts) = open_peak_ts {
                best_closed = best_closed.max(point.timestamp_ns - open_ts);
                open_peak_ts = None;
            }
            peak = point.equity;
            peak_ts = point.timestamp_ns;
        } else if open_peak_ts.is_none() {
            open_peak_ts = Some(peak_ts);
        }
    }
    if let Some(open_ts) = open_peak_ts {
        let open_span = points[points.len() - 1].timestamp_ns - open_ts;
        if open_span > best_closed {
            return Ok(DrawdownSpan {
                duration_ns: open_span,
                recovered: false,
            });
        }
    }
    Ok(DrawdownSpan {
        duration_ns: best_closed,
        recovered: true,
    })
}

/// Every equity-curve metric for one curve at once.
pub fn equity_curve_metrics(points: &[EquityPoint]) -> Result<EquityMetrics, MetricsError> {
    Ok(EquityMetrics {
        total_return: total_return(points)?,
        cagr: cagr(points)?,
        max_drawdown: max_drawdown(points)?,
        drawdown: longest_drawdown(points)?,
    })
}

/// Scale-18 half-even square root: `sqrt(value * 10^18)` rounded to nearest,
/// ties to even.
pub fn dec_sqrt(value: i128) -> Result<i128, MetricsError> {
    if value < 0 {
        return Err(MetricsError::InvalidInput);
    }
    if value == 0 {
        return Ok(0);
    }
    let wide = U256::mul_u128(value as u128, ONE as u128);
    let mut high = U256::from_u128(1);
    while square(high) < wide {
        high = high.shl1();
    }
    let mut low = U256::ZERO;
    while low < high {
        let diff = high.wrapping_sub(low);
        let step = U256::div_rem(
            diff.checked_add(U256::from_u128(1))
                .expect("doubling search bounds never overflow"),
            U256::from_u128(2),
        )
        .0;
        let mid = low
            .checked_add(step)
            .expect("bisection midpoint stays bounded");
        if square(mid) <= wide {
            low = mid;
        } else {
            high = mid.wrapping_sub(U256::from_u128(1));
        }
    }
    let below = wide.wrapping_sub(square(low));
    let above = square(low.checked_add(U256::from_u128(1)).expect("root fits"))
        .wrapping_sub(wide);
    let mut root = low;
    if above < below || (above == below && low.bit(0)) {
        root = low.checked_add(U256::from_u128(1)).expect("root fits");
    }
    if root.high != 0 || root.low > decimal::MAX_UNSCALED as u128 {
        return Err(MetricsError::OutOfRange);
    }
    Ok(root.low as i128)
}

/// Square a value known to fit in 128 bits.
fn square(value: U256) -> U256 {
    if value.high != 0 {
        return U256 {
            high: u128::MAX,
            low: u128::MAX,
        };
    }
    U256::mul_u128(value.low, value.low)
}

/// Annualized Sharpe ratio (`ddof=0`).
pub fn sharpe(
    returns: &[i128],
    periods_per_year: u32,
) -> Result<Option<f64>, MetricsError> {
    if returns.len() < 2 || periods_per_year == 0 {
        return Ok(None);
    }
    let count = returns.len() as i128 * ONE;
    let mut sum = ZERO;
    for &value in returns {
        sum = decimal::add(sum, value).map_err(decimal_error)?;
    }
    let mean = decimal::div(sum, count).map_err(decimal_error)?;
    let mut variance_sum = ZERO;
    for &value in returns {
        let deviation = decimal::sub(value, mean).map_err(decimal_error)?;
        variance_sum = decimal::add(
            variance_sum,
            decimal::mul(deviation, deviation).map_err(decimal_error)?,
        )
        .map_err(decimal_error)?;
    }
    let variance = decimal::div(variance_sum, count).map_err(decimal_error)?;
    if variance == ZERO {
        return Ok(None);
    }
    let std_dev = dec_sqrt(variance)?;
    let ratio = decimal::div(mean, std_dev).map_err(decimal_error)?;
    Ok(Some(
        unscaled_to_f64(ratio) * f64::from(periods_per_year).sqrt(),
    ))
}

/// Annualized Sortino ratio.
pub fn sortino(
    returns: &[i128],
    periods_per_year: u32,
    target: i128,
) -> Result<Option<f64>, MetricsError> {
    if returns.is_empty() || periods_per_year == 0 {
        return Ok(None);
    }
    let mut sum = ZERO;
    let mut variance_sum = ZERO;
    let mut shortfalls = 0usize;
    for &value in returns {
        sum = decimal::add(sum, value).map_err(decimal_error)?;
        if value < target {
            let shortfall = decimal::sub(value, target).map_err(decimal_error)?;
            variance_sum = decimal::add(
                variance_sum,
                decimal::mul(shortfall, shortfall).map_err(decimal_error)?,
            )
            .map_err(decimal_error)?;
            shortfalls += 1;
        }
    }
    if shortfalls == 0 {
        return Ok(None);
    }
    let count = returns.len() as i128 * ONE;
    let mean = decimal::div(sum, count).map_err(decimal_error)?;
    let variance = decimal::div(variance_sum, count).map_err(decimal_error)?;
    if variance == ZERO {
        return Ok(None);
    }
    let std_dev = dec_sqrt(variance)?;
    let ratio = decimal::div(mean, std_dev).map_err(decimal_error)?;
    Ok(Some(
        unscaled_to_f64(ratio) * f64::from(periods_per_year).sqrt(),
    ))
}

/// Aggregate realized trade PnL values.
pub fn trade_stats(trades: &[TradeRow]) -> Result<TradeStats, MetricsError> {
    let mut total = ZERO;
    let mut gross_profit = ZERO;
    let mut gross_loss = ZERO;
    let mut wins = 0usize;
    let mut losses = 0usize;
    let mut sum_win = ZERO;
    let mut sum_loss = ZERO;
    let mut largest_win: Option<i128> = None;
    let mut largest_loss: Option<i128> = None;
    for row in trades {
        let pnl = row.realized_pnl;
        total = decimal::add(total, pnl).map_err(decimal_error)?;
        if pnl > ZERO {
            wins += 1;
            sum_win = decimal::add(sum_win, pnl).map_err(decimal_error)?;
            gross_profit = decimal::add(gross_profit, pnl).map_err(decimal_error)?;
            largest_win = Some(largest_win.map_or(pnl, |current| current.max(pnl)));
        } else if pnl < ZERO {
            losses += 1;
            sum_loss = decimal::add(sum_loss, pnl).map_err(decimal_error)?;
            gross_loss = decimal::sub(gross_loss, pnl).map_err(decimal_error)?;
            largest_loss = Some(largest_loss.map_or(pnl, |current| current.min(pnl)));
        }
    }
    let trade_count = trades.len();
    Ok(TradeStats {
        trade_count,
        win_count: wins,
        loss_count: losses,
        hit_rate: if trade_count > 0 {
            Some(
                decimal::div(wins as i128 * ONE, trade_count as i128 * ONE)
                    .map_err(decimal_error)?,
            )
        } else {
            None
        },
        total_pnl: total,
        gross_profit,
        gross_loss,
        profit_factor: if gross_loss > ZERO {
            Some(decimal::div(gross_profit, gross_loss).map_err(decimal_error)?)
        } else {
            None
        },
        average_win: if wins > 0 {
            Some(decimal::div(sum_win, wins as i128 * ONE).map_err(decimal_error)?)
        } else {
            None
        },
        average_loss: if losses > 0 {
            Some(decimal::div(sum_loss, losses as i128 * ONE).map_err(decimal_error)?)
        } else {
            None
        },
        largest_win,
        largest_loss,
    })
}

/// Signed slippage of one fill against its reference price.
pub fn fill_slippage(
    quantity: i128,
    reference_price: i128,
    fill_price: i128,
) -> Result<i128, MetricsError> {
    decimal::mul(
        quantity,
        decimal::sub(fill_price, reference_price).map_err(decimal_error)?,
    )
    .map_err(decimal_error)
}

/// Aggregate fill latencies, ignoring null-latency rows.
pub fn latency_stats(fills: &[FillRow]) -> Result<LatencyStats, MetricsError> {
    let mut count = 0usize;
    let mut sum: i128 = 0;
    let mut max_ns: Option<i64> = None;
    for fill in fills {
        let Some(latency) = fill.latency_ns else {
            continue;
        };
        count += 1;
        sum = decimal::add(sum, i128::from(latency) * ONE).map_err(decimal_error)?;
        max_ns = Some(max_ns.map_or(latency, |current| current.max(latency)));
    }
    if count == 0 {
        return Ok(LatencyStats {
            count: 0,
            mean_ns: None,
            max_ns: None,
        });
    }
    Ok(LatencyStats {
        count,
        mean_ns: Some(decimal::div(sum, count as i128 * ONE).map_err(decimal_error)?),
        max_ns,
    })
}

/// Count order outcomes by exact lowercase classification.
pub fn order_outcome_counts(orders: &[OrderRow<'_>]) -> OrderOutcomeCounts {
    let mut counts = OrderOutcomeCounts {
        total: orders.len(),
        filled: 0,
        rejected: 0,
        cancelled: 0,
        other: 0,
    };
    for order in orders {
        match order.outcome {
            "filled" => counts.filled += 1,
            "rejected" => counts.rejected += 1,
            "cancelled" => counts.cancelled += 1,
            _ => counts.other += 1,
        }
    }
    counts
}

/// Aggregate fees, slippage, latency, and outcomes over the ledgers.
pub fn execution_stats(
    fills: &[FillRow],
    orders: &[OrderRow<'_>],
) -> Result<ExecutionStats, MetricsError> {
    let mut fees_total = ZERO;
    let mut slippage_total = ZERO;
    for fill in fills {
        fees_total = decimal::add(fees_total, fill.fee).map_err(decimal_error)?;
        slippage_total = decimal::add(slippage_total, fill.slippage).map_err(decimal_error)?;
    }
    Ok(ExecutionStats {
        fill_count: fills.len(),
        fees_total,
        slippage_total,
        latency: latency_stats(fills)?,
        outcomes: order_outcome_counts(orders),
    })
}

/// Trade and execution aggregates for each strategy, sorted by strategy id.
pub fn per_strategy_attribution(
    fills: &[AnnotatedFill<'_>],
    orders: &[AnnotatedOrder<'_>],
    trades: &[AnnotatedTrade<'_>],
) -> Result<Vec<StrategyAttribution>, MetricsError> {
    let mut ids: Vec<&str> = Vec::new();
    for row in fills {
        ids.push(row.strategy_id);
    }
    for row in orders {
        ids.push(row.strategy_id);
    }
    for row in trades {
        ids.push(row.strategy_id);
    }
    ids.sort_unstable();
    ids.dedup();

    let mut result = Vec::with_capacity(ids.len());
    for id in ids {
        let strategy_fills: Vec<FillRow> = fills
            .iter()
            .filter(|row| row.strategy_id == id)
            .map(|row| row.fill)
            .collect();
        let strategy_orders: Vec<OrderRow<'_>> = orders
            .iter()
            .filter(|row| row.strategy_id == id)
            .map(|row| row.order)
            .collect();
        let strategy_trades: Vec<TradeRow> = trades
            .iter()
            .filter(|row| row.strategy_id == id)
            .map(|row| row.trade)
            .collect();
        result.push(StrategyAttribution {
            strategy_id: id.to_owned(),
            trades: trade_stats(&strategy_trades)?,
            execution: execution_stats(&strategy_fills, &strategy_orders)?,
        });
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    const DAY: i64 = NANOS_PER_DAY;

    fn pt(timestamp_ns: i64, equity: i128) -> EquityPoint {
        EquityPoint {
            timestamp_ns,
            equity,
        }
    }

    fn trade(pnl: i128) -> TradeRow {
        TradeRow { realized_pnl: pnl }
    }

    fn fill(latency_ns: Option<i64>, fee: i128, slippage: i128) -> FillRow {
        FillRow {
            quantity: dec_text("1"),
            price: dec_text("100"),
            fee,
            slippage,
            latency_ns,
        }
    }

    #[test]
    fn dec_text_parses_signs_integer_and_fractional_parts() {
        assert_eq!(dec_text("1"), ONE);
        assert_eq!(dec_text("-1"), -ONE);
        assert_eq!(dec_text("0.4"), 400_000_000_000_000_000);
        assert_eq!(dec_text("4.0"), 4 * ONE);
        assert_eq!(dec_text("0"), 0);
    }

    #[test]
    fn total_return_and_cagr_match_the_hand_computed_vectors() {
        let curve = [pt(0, dec_text("100")), pt(DAY, dec_text("80")), pt(2 * DAY, dec_text("121"))];
        assert_eq!(total_return(&curve).unwrap(), Some(dec_text("0.21")));
        assert_eq!(total_return(&[pt(0, dec_text("100"))]).unwrap(), None);
        assert_eq!(
            total_return(&[pt(0, dec_text("0")), pt(DAY, dec_text("50"))]).unwrap(),
            None
        );
        assert_eq!(total_return(&[]).unwrap(), None);

        let year = [pt(0, dec_text("100")), pt(365 * DAY, dec_text("121"))];
        assert!((cagr(&year).unwrap().unwrap() - 1.21).abs() < 1e-12);
        let half = [pt(0, dec_text("100")), pt(365 * DAY / 2, dec_text("121"))];
        assert!((cagr(&half).unwrap().unwrap() - 1.21 * 1.21).abs() < 1e-12);
        assert_eq!(
            cagr(&[pt(0, dec_text("100")), pt(DAY, dec_text("0"))]).unwrap(),
            None
        );
    }

    #[test]
    fn drawdown_metrics_pin_the_hand_computed_vectors() {
        let curve = [
            pt(0, dec_text("100")),
            pt(DAY, dec_text("80")),
            pt(2 * DAY, dec_text("120")),
            pt(3 * DAY, dec_text("90")),
        ];
        assert_eq!(max_drawdown(&curve).unwrap(), dec_text("0.25"));
        assert_eq!(
            max_drawdown(&[pt(0, dec_text("100")), pt(DAY, dec_text("120"))]).unwrap(),
            dec_text("0")
        );
        assert_eq!(
            max_drawdown(&[pt(0, dec_text("100")), pt(DAY, dec_text("0"))]).unwrap(),
            dec_text("1")
        );
        assert!(max_drawdown(&[pt(0, dec_text("100")), pt(DAY, dec_text("-1"))]).is_err());

        let span = longest_drawdown(&curve).unwrap();
        assert_eq!(span.duration_ns, 2 * DAY);
        assert!(span.recovered);
        let open = [pt(0, dec_text("100")), pt(DAY, dec_text("50")), pt(2 * DAY, dec_text("0"))];
        let span = longest_drawdown(&open).unwrap();
        assert_eq!(span.duration_ns, 2 * DAY);
        assert!(!span.recovered);
        let bundled = equity_curve_metrics(&curve).unwrap();
        assert_eq!(bundled.total_return, total_return(&curve).unwrap());
        assert_eq!(bundled.max_drawdown, max_drawdown(&curve).unwrap());
    }

    #[test]
    fn scale18_square_root_rounds_half_even_against_exact_squares() {
        assert_eq!(dec_sqrt(dec_text("0.09")).unwrap(), 300_000_000_000_000_000);
        assert_eq!(dec_sqrt(ONE).unwrap(), ONE);
        assert_eq!(dec_sqrt(0).unwrap(), 0);
        assert_eq!(dec_sqrt(-1), Err(MetricsError::InvalidInput));
        assert_eq!(dec_sqrt(dec_text("2.25")).unwrap(), 1_500_000_000_000_000_000);
    }

    #[test]
    fn sharpe_and_sortino_pin_the_hand_computed_vectors() {
        let returns = [dec_text("0.05"), dec_text("0.05"), dec_text("-0.01"), dec_text("-0.01")];
        let expected = (2.0 / 3.0) * 252.0f64.sqrt();
        assert!((sharpe(&returns, 252).unwrap().unwrap() - expected).abs() < 1e-12);
        assert_eq!(sharpe(&[dec_text("0.02"), dec_text("0.02")], 252).unwrap(), None);
        assert_eq!(sharpe(&[dec_text("0.02")], 252).unwrap(), None);

        let returns = [dec_text("0.03"), dec_text("-0.01"), dec_text("0.05"), dec_text("-0.03")];
        let downside = ((0.0001 + 0.0009) / 4.0f64).sqrt();
        assert!((sortino(&returns, 4, 0).unwrap().unwrap() - 0.01 / downside * 2.0).abs() < 1e-12);
        assert_eq!(sortino(&[dec_text("0.01"), dec_text("0.02")], 4, 0).unwrap(), None);
    }

    #[test]
    fn trade_stats_pin_the_five_trade_vector() {
        let trades = [
            trade(dec_text("100")),
            trade(dec_text("-50")),
            trade(dec_text("30")),
            trade(dec_text("-20")),
            trade(dec_text("0")),
        ];
        let stats = trade_stats(&trades).unwrap();
        assert_eq!(stats.trade_count, 5);
        assert_eq!(stats.win_count, 2);
        assert_eq!(stats.loss_count, 2);
        assert_eq!(stats.hit_rate, Some(dec_text("0.4")));
        assert_eq!(stats.total_pnl, dec_text("60"));
        assert_eq!(stats.gross_profit, dec_text("130"));
        assert_eq!(stats.gross_loss, dec_text("70"));
        assert_eq!(stats.profit_factor, Some(1_857_142_857_142_857_143));
        assert_eq!(stats.average_win, Some(dec_text("65")));
        assert_eq!(stats.average_loss, Some(dec_text("-35")));
        assert_eq!(stats.largest_win, Some(dec_text("100")));
        assert_eq!(stats.largest_loss, Some(dec_text("-50")));

        let no_loss = [trade(dec_text("10")), trade(dec_text("5"))];
        let stats = trade_stats(&no_loss).unwrap();
        assert_eq!(stats.profit_factor, None);
        assert_eq!(stats.hit_rate, Some(ONE));
        let empty = trade_stats(&[]).unwrap();
        assert_eq!(empty.trade_count, 0);
        assert_eq!(empty.hit_rate, None);
    }

    #[test]
    fn execution_stats_pin_the_hand_computed_vectors() {
        assert_eq!(fill_slippage(dec_text("2"), dec_text("100"), dec_text("100.5")).unwrap(), dec_text("1.0"));
        assert_eq!(fill_slippage(dec_text("-3"), dec_text("200"), dec_text("199.5")).unwrap(), dec_text("1.5"));

        let fills = [
            fill(Some(100), 0, 0),
            fill(Some(200), 0, 0),
            fill(None, 0, 0),
            fill(Some(300), 0, 0),
        ];
        let stats = latency_stats(&fills).unwrap();
        assert_eq!(stats.count, 3);
        assert_eq!(stats.mean_ns, Some(dec_text("200")));
        assert_eq!(stats.max_ns, Some(300));
        assert_eq!(latency_stats(&[]).unwrap().mean_ns, None);

        let orders = [
            OrderRow { outcome: "filled" },
            OrderRow { outcome: "filled" },
            OrderRow { outcome: "rejected" },
            OrderRow { outcome: "cancelled" },
            OrderRow { outcome: "expired" },
        ];
        let counts = order_outcome_counts(&orders);
        assert_eq!(counts.total, 5);
        assert_eq!(counts.filled, 2);
        assert_eq!(counts.rejected, 1);
        assert_eq!(counts.cancelled, 1);
        assert_eq!(counts.other, 1);

        let fills = [fill(Some(10), dec_text("1.5"), dec_text("1.0")), fill(Some(30), dec_text("2.5"), dec_text("0.2"))];
        let orders = [OrderRow { outcome: "filled" }, OrderRow { outcome: "rejected" }];
        let stats = execution_stats(&fills, &orders).unwrap();
        assert_eq!(stats.fill_count, 2);
        assert_eq!(stats.fees_total, dec_text("4.0"));
        assert_eq!(stats.slippage_total, dec_text("1.2"));
        assert_eq!(stats.latency.mean_ns, Some(dec_text("20")));
        assert_eq!(stats.outcomes.total, 2);
    }

    #[test]
    fn per_strategy_attribution_groups_and_zeroes_absent_strategies() {
        let fills = [
            AnnotatedFill {
                strategy_id: "alpha",
                fill: fill(Some(10), dec_text("1"), dec_text("0.5")),
            },
            AnnotatedFill {
                strategy_id: "beta",
                fill: fill(Some(20), dec_text("2"), dec_text("0")),
            },
        ];
        let orders = [AnnotatedOrder {
            strategy_id: "alpha",
            order: OrderRow { outcome: "filled" },
        }];
        let trades = [AnnotatedTrade {
            strategy_id: "alpha",
            trade: trade(dec_text("10")),
        }];
        let attribution = per_strategy_attribution(&fills, &orders, &trades).unwrap();
        assert_eq!(attribution.len(), 2);
        assert_eq!(attribution[0].strategy_id, "alpha");
        assert_eq!(attribution[1].strategy_id, "beta");
        assert_eq!(attribution[0].trades.total_pnl, dec_text("10"));
        assert_eq!(attribution[0].execution.fees_total, dec_text("1"));
        assert_eq!(attribution[0].execution.slippage_total, dec_text("0.5"));
        assert_eq!(attribution[0].execution.outcomes.filled, 1);
        assert_eq!(attribution[1].trades.trade_count, 0);
        assert_eq!(attribution[1].execution.fees_total, dec_text("2"));
        assert_eq!(attribution[1].execution.outcomes.total, 0);
        assert!(per_strategy_attribution(&[], &[], &[]).unwrap().is_empty());
    }
}
