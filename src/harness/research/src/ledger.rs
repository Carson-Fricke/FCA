//! Owns fill-to-trade reconstruction and exact valuation/report projections.
//! It consumes replay outcomes; it does not execute strategies or publish files.
use std::collections::BTreeMap;

use harness_contracts::progress::ProgressReporter;
use num::Decimal18;
use num::DecimalError;

use crate::error::BacktestJobError;
use backtest::backtest::config::BacktestConfig;
use backtest::backtest::outcome::BacktestOutcome;
use backtest::backtest::valuation::ValuationHistory;
use backtest::backtest::valuation::ValuationRow;
use backtest::backtest::valuation::REPORT_INTERVAL_NS;
use backtest::results::metrics::execution_stats;
use backtest::results::metrics::trade_stats;
use backtest::results::metrics::EquityMetrics;
use backtest::results::metrics::EquityPoint;
use backtest::results::metrics::ExecutionStats;
use backtest::results::metrics::FillRow;
use backtest::results::metrics::MetricsError;
use backtest::results::metrics::OrderRow;
use backtest::results::metrics::Outcome;
use backtest::results::metrics::TradeRow;
use backtest::results::metrics::TradeStats;
use harness_contracts::canonical_json::JsonValue;

/// Counts rendered in the `metrics.json` `counts` object.
pub(super) struct JobCounts {
    pub(super) market_events: u64,
    pub(super) dispatched: u64,
    pub(super) bar_frames: Option<u64>,
    pub(super) orders: u64,
    pub(super) fills: u64,
}

/// The deterministic metrics the job derives from the engine's fills. Every
/// statistic is computed once here, before any artifact is written.
pub(super) struct JobLedger {
    pub(super) rows: backtest::backtest::history::History<ValuationRow>,
    pub(super) sample_count: u64,
    pub(super) trades: ClosedTrades,
    pub(super) trade_stats: TradeStats,
    pub(super) equity_metrics: EquityMetrics,
    pub(super) chart: Vec<EquityPoint>,
    pub(super) net_pnl: Decimal18,
    pub(super) gross_pnl: Decimal18,
    pub(super) execution: ExecutionStats,
    pub(super) initial_cash: Decimal18,
    pub(super) final_cash: Decimal18,
    pub(super) final_equity: Decimal18,
    pub(super) peak_gross_exposure: Decimal18,
    pub(super) peak_gross_exposure_fraction: Outcome<Decimal18>,
}

/// One normalized fill shared by the injected and native engine paths.
pub(super) struct LedgerFill {
    pub(super) fill_id: String,
    pub(super) order_id: String,
    pub(super) symbol: String,
    pub(super) side: String,
    pub(super) quantity: String,
    pub(super) price: String,
    pub(super) fee: String,
    pub(super) timestamp_ns: i64,
    pub(super) sequence: u64,
}

/// One normalized order shared by the injected and native engine paths.
pub(super) struct LedgerOrder {
    pub(super) order_id: String,
    pub(super) symbol: String,
    pub(super) side: String,
    pub(super) quantity: String,
    pub(super) status: String,
}

/// Parse one configuration decimal. A malformed value fails the job; it never
/// defaults to zero, which would silently change PnL.
pub(super) fn ledger_decimal(value: &str, field: &str) -> Result<Decimal18, BacktestJobError> {
    value.parse::<Decimal18>().map_err(|error| {
        BacktestJobError::Engine(format!("{field} {value:?} is not a decimal: {error}"))
    })
}

/// Parse one field of a fill. A malformed value fails the job as a
/// [`BacktestJobError::MalformedFill`]; it never defaults to zero.
pub(super) fn fill_decimal(
    fill: &LedgerFill,
    field: &str,
    value: &str,
) -> Result<Decimal18, BacktestJobError> {
    value.parse::<Decimal18>().map_err(|error| {
        malformed_fill(
            fill,
            format!("{field} {value:?} is not a decimal in decimal128(38,18): {error}"),
        )
    })
}

pub(super) fn malformed_fill(fill: &LedgerFill, detail: impl Into<String>) -> BacktestJobError {
    BacktestJobError::MalformedFill {
        fill_id: fill.fill_id.clone(),
        detail: detail.into(),
    }
}

/// Map one exact operation that left the decimal bound onto the job error.
pub(super) fn overflow_ledger(
    result: Result<Decimal18, DecimalError>,
    operation: &str,
) -> Result<Decimal18, BacktestJobError> {
    result.map_err(|_| {
        BacktestJobError::Overflow(format!("{operation} overflows the decimal128(38,18) bound"))
    })
}

pub(super) fn metrics_overflow(error: MetricsError) -> BacktestJobError {
    BacktestJobError::Overflow(error.to_string())
}

/// Direction of one fill: `true` for a buy, `false` for a sell. The side is an
/// exact lowercase comparison; any other value fails the job.
pub(super) fn fill_is_buy(fill: &LedgerFill) -> Result<bool, BacktestJobError> {
    match fill.side.as_str() {
        "buy" => Ok(true),
        "sell" => Ok(false),
        other => Err(malformed_fill(
            fill,
            format!("side {other:?} is neither \"buy\" nor \"sell\""),
        )),
    }
}

pub(super) fn ledger_fills_from_outcome(outcome: &BacktestOutcome) -> Vec<LedgerFill> {
    outcome
        .fills
        .iter()
        .map(|fill| LedgerFill {
            fill_id: fill.fill_id.clone(),
            order_id: fill.order_id.clone(),
            symbol: fill.symbol.clone(),
            side: fill.side.clone(),
            quantity: fill.quantity.to_string(),
            price: fill.price.to_string(),
            fee: fill.fee.to_string(),
            timestamp_ns: fill.timestamp_ns,
            sequence: fill.sequence,
        })
        .collect()
}

pub(super) fn ledger_orders_from_outcome(outcome: &BacktestOutcome) -> Vec<LedgerOrder> {
    outcome
        .orders
        .iter()
        .map(|order| LedgerOrder {
            order_id: order.order_id.clone(),
            symbol: order.symbol.clone(),
            side: order.side.clone(),
            quantity: order.quantity.to_string(),
            status: if order.status == "canceled" {
                "cancelled".into()
            } else {
                order.status.clone()
            },
        })
        .collect()
}

/// Closed trade slices plus the fees still attached to open lots at run end.
pub(super) struct ClosedTrades {
    pub(super) rows: Vec<TradeRow>,
    pub(super) open_fees: Decimal18,
}

/// Derive closed trades from one fill ledger with FIFO matching.
///
/// Fills are ordered by `(sequence, timestamp)`. A fill closes opposite lots
/// from the front of its symbol's queue and opens a lot with any unmatched
/// remainder, so position flips keep the remainder. Each slice carries its
/// share of both fees by fill quantity (`fill_quantity_share.v1`), so the
/// closed-slice fees plus `open_fees` equal the total fees exactly.
pub(super) fn closed_trades(fills: &[LedgerFill]) -> Result<ClosedTrades, BacktestJobError> {
    use harness_trading::fills::trades::{TradeFill, TradeLedger};
    let mut ordered: Vec<_> = fills.iter().collect();
    ordered.sort_by_key(|fill| (fill.sequence, fill.timestamp_ns));
    let mut ledger = TradeLedger::default();
    for fill in ordered {
        let quantity = fill_decimal(fill, "quantity", &fill.quantity)?;
        let fee = fill_decimal(fill, "fee", &fill.fee)?;
        if quantity.is_zero() && fee.is_zero() {
            continue;
        }
        if quantity <= Decimal18::ZERO {
            return Err(malformed_fill(fill, "quantity must be positive"));
        }
        let price = fill_decimal(fill, "price", &fill.price)?;
        if price <= Decimal18::ZERO {
            return Err(malformed_fill(fill, "price must be positive"));
        }
        ledger
            .apply(&TradeFill {
                fill_id: fill.fill_id.clone(),
                symbol: fill.symbol.clone(),
                buy: fill_is_buy(fill)?,
                quantity,
                price,
                fee,
                timestamp_ns: fill.timestamp_ns,
                sequence: fill.sequence,
            })
            .map_err(|_| {
                BacktestJobError::Overflow(
                    "closed-trade arithmetic exceeds decimal128(38,18)".into(),
                )
            })?;
    }
    let open_fees = overflow_ledger(ledger.open_fees(), "open fees")?;
    Ok(ClosedTrades {
        rows: ledger.rows.as_ref().clone(),
        open_fees,
    })
}

pub(super) fn build_ledger(
    configuration: &BacktestConfig,
    fills: &[LedgerFill],
    orders: &[LedgerOrder],
    valuations: &ValuationHistory,
    _reporter: Option<&ProgressReporter>,
) -> Result<JobLedger, BacktestJobError> {
    let (equity_metrics, rows) = valuations
        .summarize()
        .map_err(|e| BacktestJobError::Io(e.to_string()))?;
    let ledger_fills: Vec<_> = fills
        .iter()
        .map(|fill| {
            Ok(FillRow {
                fill_id: fill.fill_id.clone(),
                order_id: fill.order_id.clone(),
                strategy_id: String::new(),
                quantity: fill_decimal(fill, "quantity", &fill.quantity)?,
                price: fill_decimal(fill, "price", &fill.price)?,
                fee: fill_decimal(fill, "fee", &fill.fee)?,
                latency_ns: None,
            })
        })
        .collect::<Result<_, BacktestJobError>>()?;
    let ledger_orders: Vec<OrderRow> = orders
        .iter()
        .map(|order| OrderRow {
            order_id: order.order_id.clone(),
            strategy_id: String::new(),
            outcome: order.status.clone(),
        })
        .collect();
    let trades = closed_trades(fills)?;
    let trade_stats = trade_stats(&trades.rows, trades.open_fees).map_err(metrics_overflow)?;
    let execution = execution_stats(&ledger_fills, &ledger_orders).map_err(metrics_overflow)?;
    let net_pnl = overflow_ledger(
        rows.last()
            .expect("opening capital sample")
            .equity
            .checked_sub(ledger_decimal(&configuration.initial_cash, "initial_cash")?),
        "marked net PnL",
    )?;
    let gross_pnl = overflow_ledger(
        net_pnl.checked_add(execution.fees_total),
        "marked gross PnL",
    )?;
    let stride = rows.len().div_ceil(2000).max(1);
    let mut chart = Vec::new();
    for (index, row) in rows
        .iter()
        .map_err(|e| BacktestJobError::Io(e.to_string()))?
        .enumerate()
    {
        let row = row.map_err(|e| BacktestJobError::Io(e.to_string()))?;
        if index.is_multiple_of(stride) || index + 1 == rows.len() {
            chart.push(row.point());
        }
    }
    let last = rows.last().expect("opening capital sample");
    let initial_cash = ledger_decimal(&configuration.initial_cash, "initial_cash")?;
    Ok(JobLedger {
        initial_cash,
        final_cash: last.cash,
        final_equity: last.equity,
        peak_gross_exposure: valuations.peak_gross_exposure(),
        peak_gross_exposure_fraction: if initial_cash.is_positive() {
            valuations
                .peak_gross_exposure()
                .checked_div(initial_cash)
                .map_err(|_| "arithmetic_overflow")
        } else {
            Err("nonpositive_initial_capital")
        },
        rows,
        sample_count: valuations.sample_count(),
        trades,
        trade_stats,
        equity_metrics,
        chart,
        net_pnl,
        gross_pnl,
        execution,
    })
}

pub(super) fn decimal_opt(value: Option<Decimal18>) -> JsonValue {
    match value {
        Some(value) => JsonValue::String(value.to_string()),
        None => JsonValue::Null,
    }
}

/// A decimal in the canonical Decimal18 text (Python `str(Decimal)` form).
pub(super) fn decimal_text(value: Decimal18) -> JsonValue {
    JsonValue::String(value.to_string())
}

/// The reason an optional statistic is null.
pub(super) fn reason_opt(reason: Option<&'static str>) -> JsonValue {
    match reason {
        Some(reason) => JsonValue::string(reason),
        None => JsonValue::Null,
    }
}

/// An equity decimal: its canonical text, or null when it is undefined.
pub(super) fn decimal_outcome(outcome: Outcome<Decimal18>) -> JsonValue {
    match outcome {
        Ok(value) => JsonValue::String(value.to_string()),
        Err(_) => JsonValue::Null,
    }
}

/// An equity ratio as a decimal string with 18 fractional digits, or null when
/// it is undefined.
pub(super) fn float_outcome(outcome: Outcome<f64>) -> JsonValue {
    match outcome {
        Ok(value) => JsonValue::String(format!("{value:.18}")),
        Err(_) => JsonValue::Null,
    }
}

/// The closed reason an equity metric is undefined, or null when it is defined.
fn reason_outcome<T>(outcome: Outcome<T>) -> JsonValue {
    match outcome {
        Ok(_) => JsonValue::Null,
        Err(reason) => JsonValue::string(reason),
    }
}

/// One closed trade slice as its canonical JSON row.
pub(super) fn trade_row_json(row: &TradeRow) -> JsonValue {
    JsonValue::object([
        ("trade_id", JsonValue::UInt(row.trade_id)),
        ("symbol", JsonValue::string(row.symbol.as_str())),
        ("side", JsonValue::string(row.side.as_str())),
        ("quantity", decimal_text(row.quantity)),
        ("entry_sequence", JsonValue::UInt(row.entry_sequence)),
        ("exit_sequence", JsonValue::UInt(row.exit_sequence)),
        ("entry_timestamp_ns", JsonValue::Int(row.entry_timestamp_ns)),
        ("exit_timestamp_ns", JsonValue::Int(row.exit_timestamp_ns)),
        ("entry_price", decimal_text(row.entry_price)),
        ("exit_price", decimal_text(row.exit_price)),
        ("gross_pnl", decimal_text(row.gross_pnl)),
        ("entry_fee", decimal_text(row.entry_fee)),
        ("exit_fee", decimal_text(row.exit_fee)),
        ("fees", decimal_text(row.fees)),
        ("net_pnl", decimal_text(row.net_pnl)),
        (
            "gross_return_bps",
            decimal_opt(row.return_bps(false).expect("validated trade return")),
        ),
        (
            "net_return_bps",
            decimal_opt(row.return_bps(true).expect("validated trade return")),
        ),
    ])
}

pub(super) fn metrics_json(
    job_id: &str,
    status: &str,
    counts: &JobCounts,
    ledger: &JobLedger,
    custom_metrics: &BTreeMap<String, String>,
    equity: &EquityMetrics,
) -> JsonValue {
    let execution = &ledger.execution;
    let trades = &ledger.trade_stats;
    let mut count_values = BTreeMap::from([
        (
            "market_events".to_string(),
            JsonValue::UInt(counts.market_events),
        ),
        ("dispatched".to_string(), JsonValue::UInt(counts.dispatched)),
        ("orders".to_string(), JsonValue::UInt(counts.orders)),
        ("fills".to_string(), JsonValue::UInt(counts.fills)),
    ]);
    if let Some(frames) = counts.bar_frames {
        count_values.insert("bar_frames".to_string(), JsonValue::UInt(frames));
    }
    JsonValue::object([
        ("job_id", JsonValue::String(job_id.to_string())),
        ("status", JsonValue::string(status)),
        (
            "capital",
            JsonValue::object([
                ("initial_cash", decimal_text(ledger.initial_cash)),
                ("final_cash", decimal_text(ledger.final_cash)),
                ("final_equity", decimal_text(ledger.final_equity)),
                (
                    "peak_gross_exposure",
                    decimal_text(ledger.peak_gross_exposure),
                ),
                (
                    "peak_gross_exposure_fraction",
                    decimal_outcome(ledger.peak_gross_exposure_fraction),
                ),
                (
                    "peak_gross_exposure_fraction_reason",
                    reason_outcome(ledger.peak_gross_exposure_fraction),
                ),
            ]),
        ),
        ("counts", JsonValue::Object(count_values)),
        (
            "equity",
            JsonValue::object([
                ("samples", JsonValue::UInt(ledger.sample_count)),
                (
                    "retained_samples",
                    JsonValue::UInt(ledger.rows.len() as u64),
                ),
                ("report_interval_ns", JsonValue::Int(REPORT_INTERVAL_NS)),
                ("metric_sample_basis", JsonValue::string("causal_dispatch")),
                ("constant_state_compaction", JsonValue::Bool(true)),
                ("net_pnl", decimal_text(ledger.net_pnl)),
                ("net_pnl_reason", JsonValue::Null),
                ("gross_pnl", decimal_text(ledger.gross_pnl)),
                ("gross_pnl_reason", JsonValue::Null),
                ("fees", decimal_text(execution.fees_total)),
                ("total_return", decimal_outcome(equity.total_return)),
                ("total_return_reason", reason_outcome(equity.total_return)),
                ("cagr", float_outcome(equity.cagr)),
                ("cagr_reason", reason_outcome(equity.cagr)),
                ("max_drawdown", decimal_outcome(equity.max_drawdown)),
                ("max_drawdown_reason", reason_outcome(equity.max_drawdown)),
                (
                    "drawdown_duration_ns",
                    match equity.drawdown {
                        Ok(span) => JsonValue::Int(span.duration_ns),
                        Err(_) => JsonValue::Null,
                    },
                ),
                (
                    "drawdown_recovered",
                    match equity.drawdown {
                        Ok(span) => JsonValue::Bool(span.recovered),
                        Err(_) => JsonValue::Null,
                    },
                ),
                ("sharpe_per_sample", float_outcome(equity.sharpe_per_sample)),
                (
                    "sharpe_per_sample_reason",
                    reason_outcome(equity.sharpe_per_sample),
                ),
            ]),
        ),
        (
            "execution",
            JsonValue::object([
                ("fill_count", JsonValue::UInt(execution.fill_count as u64)),
                (
                    "fees_total",
                    JsonValue::String(execution.fees_total.to_string()),
                ),
                ("slippage_total", JsonValue::Null),
                ("slippage_total_reason", JsonValue::string("not_modeled")),
                (
                    "outcomes",
                    JsonValue::object([
                        ("total", JsonValue::UInt(execution.outcomes.total as u64)),
                        ("filled", JsonValue::UInt(execution.outcomes.filled as u64)),
                        (
                            "rejected",
                            JsonValue::UInt(execution.outcomes.rejected as u64),
                        ),
                        (
                            "cancelled",
                            JsonValue::UInt(execution.outcomes.cancelled as u64),
                        ),
                        ("other", JsonValue::UInt(execution.outcomes.other as u64)),
                    ]),
                ),
            ]),
        ),
        (
            "trades",
            JsonValue::object([
                ("schema", JsonValue::string("closed_trades.v1")),
                (
                    "fee_allocation",
                    JsonValue::string("fill_quantity_share.v1"),
                ),
                ("trade_count", JsonValue::UInt(trades.trade_count as u64)),
                ("win_count", JsonValue::UInt(trades.win_count as u64)),
                ("loss_count", JsonValue::UInt(trades.loss_count as u64)),
                ("zero_count", JsonValue::UInt(trades.zero_count as u64)),
                ("gross_pnl", decimal_text(trades.gross_pnl)),
                ("fees", decimal_text(trades.fees)),
                ("net_pnl", decimal_text(trades.net_pnl)),
                ("open_fees", decimal_text(trades.open_fees)),
                ("gross_profit", decimal_text(trades.gross_profit)),
                ("gross_loss", decimal_text(trades.gross_loss)),
                ("net_profit", decimal_text(trades.net_profit)),
                ("net_loss", decimal_text(trades.net_loss)),
                ("profit_factor", decimal_opt(trades.profit_factor)),
                (
                    "profit_factor_reason",
                    reason_opt(trades.profit_factor_reason),
                ),
                ("hit_rate", decimal_opt(trades.hit_rate)),
                ("hit_rate_reason", reason_opt(trades.hit_rate_reason)),
                ("average_net_win", decimal_opt(trades.average_net_win)),
                ("average_net_loss", decimal_opt(trades.average_net_loss)),
                ("largest_net_win", decimal_opt(trades.largest_net_win)),
                ("largest_net_loss", decimal_opt(trades.largest_net_loss)),
                (
                    "rows",
                    JsonValue::Array(ledger.trades.rows.iter().map(trade_row_json).collect()),
                ),
            ]),
        ),
        (
            "custom",
            JsonValue::object(
                custom_metrics
                    .iter()
                    .map(|(name, value)| (name.clone(), JsonValue::String(value.clone()))),
            ),
        ),
    ])
}
