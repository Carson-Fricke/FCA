//! Owns current research artifact paths, Parquet/report writes and publication progress.
//! It consumes outcomes and ledger projections without replaying or acquiring market data.
use std::collections::BTreeMap;
use std::path::Path;
use std::path::PathBuf;

use harness_contracts::progress::ExecutionProgress;
use harness_contracts::progress::ExecutionStage;
use harness_contracts::progress::ProgressReporter;
use num::Decimal18;

use crate::error::BacktestJobError;
use crate::ledger::*;
use backtest::backtest::config::BacktestConfig;
use backtest::backtest::outcome::BacktestOutcome;
use backtest::results::metrics::EquityMetrics;
use backtest::results::recorder::ActionKind;
use backtest::results::recorder::ActionRecord;
use backtest::results::recorder::FillRecord;
use backtest::results::recorder::PortfolioEquityRecord;
use backtest::results::recorder::RecorderConfig;
use backtest::results::recorder::RunRecorder;
use backtest::results::report::render_report;
use backtest::results::report::ReportHeader;
use harness_contracts::canonical_json::JsonValue;

/// Write the canonical artifact and complete result set for one native outcome.
///
/// Ledger arithmetic is validated before encoding. The exact variance pass
/// shares the artifact reader; immutable upload and atomic visibility belong
/// to the result store, after this complete local result set has been written.
pub(super) fn write_run_artifacts(
    configuration: &BacktestConfig,
    outcome: &BacktestOutcome,
    output_dir: Option<&Path>,
    default_subdir: &str,
    artifact_name: &str,
    reporter: Option<&ProgressReporter>,
) -> Result<PathBuf, BacktestJobError> {
    for program in outcome.exploratory_outputs.values() {
        for output in program.as_object().into_iter().flat_map(|map| map.values()) {
            if output.get("kind").and_then(serde_json::Value::as_str) == Some("fitted_state") {
                let matches_job = output
                    .get("training_job_id")
                    .and_then(serde_json::Value::as_str)
                    == Some(outcome.manifest.job_id.as_str());
                let start = output
                    .get("training_start_ns")
                    .and_then(serde_json::Value::as_i64);
                let end = output
                    .get("training_end_ns")
                    .and_then(serde_json::Value::as_i64);
                let available = output
                    .get("available_at_ns")
                    .and_then(serde_json::Value::as_i64);
                if !matches_job
                    || start.is_none_or(|v| v < configuration.start_ns)
                    || end.is_none_or(|v| v > configuration.end_ns)
                    || available.is_none_or(|v| v > configuration.end_ns)
                {
                    return Err(BacktestJobError::Artifact(
                        "fitted state must reference its actual training job and executed window"
                            .into(),
                    ));
                }
            }
        }
    }
    let fills = ledger_fills_from_outcome(outcome);
    let orders = ledger_orders_from_outcome(outcome);
    let ledger = build_ledger(
        configuration,
        &fills,
        &orders,
        &outcome.valuations,
        reporter,
    )?;
    report_results(
        reporter,
        ExecutionStage::ComputingResults,
        ledger.sample_count,
        true,
    )?;
    report_results(reporter, ExecutionStage::WritingArtifacts, 0, false)?;
    let directory = output_dir.map_or_else(
        || {
            PathBuf::from(".local")
                .join(default_subdir)
                .join(&configuration.job_id)
        },
        Path::to_path_buf,
    );
    std::fs::create_dir_all(&directory).map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let artifact_path = directory.join(artifact_name);
    let mut payload = outcome.artifact_json().into_bytes();
    payload.push(b'\n');
    std::fs::write(&artifact_path, payload)
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    write_native_results(
        &directory,
        configuration,
        outcome,
        &fills,
        &orders,
        &ledger,
        reporter,
    )?;
    let outputs = serde_json::json!({"schema_version":1,"job_id":outcome.manifest.job_id,"programs":outcome.exploratory_outputs});
    std::fs::write(
        directory.join("exploratory_outputs.json"),
        serde_json::to_vec_pretty(&outputs).map_err(|e| BacktestJobError::Io(e.to_string()))?,
    )
    .map_err(|e| BacktestJobError::Io(e.to_string()))?;
    report_results(
        reporter,
        ExecutionStage::WritingArtifacts,
        ledger.rows.len() as u64,
        true,
    )?;
    Ok(artifact_path)
}

/// Write the complete result set for one native engine run.
///
/// In addition to the canonical `backtest.json` the caller has already written,
/// this emits `metrics.json`, the offline `report.html`, and the recorder's
/// linked Parquet artifact trail (including the `equity` time series).
///
/// # Errors
///
/// Returns [`BacktestJobError::Artifact`] or [`BacktestJobError::Engine`] when an
/// artifact cannot be written.
pub(super) fn write_native_results(
    directory: &Path,
    configuration: &BacktestConfig,
    outcome: &BacktestOutcome,
    fills: &[LedgerFill],
    orders: &[LedgerOrder],
    ledger: &JobLedger,
    reporter: Option<&ProgressReporter>,
) -> Result<(), BacktestJobError> {
    let counts = JobCounts {
        market_events: outcome.market_event_count,
        dispatched: outcome.dispatched_count,
        bar_frames: (configuration.shell == "study").then_some(outcome.bar_frame_count),
        orders: outcome.order_count,
        fills: outcome.fills.len() as u64,
    };
    let equity = record_artifacts(
        &directory.join("artifacts"),
        configuration,
        orders,
        fills,
        ledger,
        &outcome.custom_metrics,
        reporter,
    )?;
    write_metrics(
        directory,
        &outcome.manifest.job_id,
        outcome.status.value(),
        &counts,
        ledger,
        &outcome.custom_metrics,
        &equity,
    )?;
    write_report(
        directory,
        &outcome.manifest.job_id,
        configuration.seed,
        ledger,
        &equity,
    )?;
    Ok(())
}

pub(super) fn report_results(
    reporter: Option<&ProgressReporter>,
    stage: ExecutionStage,
    rows: u64,
    completed: bool,
) -> Result<(), BacktestJobError> {
    if let Some(report) = reporter {
        let mut progress = ExecutionProgress::stage(stage);
        progress.rows = rows;
        progress.completed = completed;
        report(progress).map_err(BacktestJobError::Io)?;
    }
    Ok(())
}

pub(super) fn write_metrics(
    directory: &Path,
    job_id: &str,
    status: &str,
    counts: &JobCounts,
    ledger: &JobLedger,
    custom_metrics: &BTreeMap<String, String>,
    equity: &EquityMetrics,
) -> Result<PathBuf, BacktestJobError> {
    let path = directory.join("metrics.json");
    let mut payload = metrics_json(job_id, status, counts, ledger, custom_metrics, equity)
        .to_canonical_string()
        .into_bytes();
    payload.push(b'\n');
    std::fs::write(&path, payload)
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    Ok(path)
}

pub(super) fn write_report(
    directory: &Path,
    job_id: &str,
    seed: u64,
    ledger: &JobLedger,
    equity: &EquityMetrics,
) -> Result<PathBuf, BacktestJobError> {
    let mut header = ReportHeader::new(job_id);
    header.seed = seed.to_string();
    let document = render_report(&header, equity, &ledger.chart, &ledger.trade_stats);
    let path = directory.join("report.html");
    std::fs::write(&path, document.into_bytes())
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    Ok(path)
}

pub(super) fn record_artifacts(
    directory: &Path,
    configuration: &BacktestConfig,
    orders: &[LedgerOrder],
    fills: &[LedgerFill],
    ledger: &JobLedger,
    custom_metrics: &BTreeMap<String, String>,
    reporter: Option<&ProgressReporter>,
) -> Result<EquityMetrics, BacktestJobError> {
    let config = RecorderConfig::new(&configuration.job_id, directory)
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?
        .with_seed(configuration.seed as i64)
        .with_account_id("backtest")
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    let mut recorder =
        RunRecorder::new(config).map_err(|error| BacktestJobError::Engine(error.to_string()))?;
    let mut action_ids: BTreeMap<String, String> = BTreeMap::new();
    for (index, order) in orders.iter().enumerate() {
        let action_id = recorder
            .record_action(&ActionRecord {
                kind: Some(ActionKind::Submit),
                symbol: Some(order.symbol.clone()),
                side: Some(order.side.clone()),
                quantity: Some(order.quantity.clone()),
                order_id: Some(order.order_id.clone()),
                at_ns: Some(configuration.start_ns + index as i64),
                ..ActionRecord::default()
            })
            .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
        action_ids.insert(order.order_id.clone(), action_id);
    }
    for fill in fills {
        recorder
            .record_fill(&FillRecord {
                fill_id: fill.fill_id.clone(),
                sequence: fill.sequence,
                timestamp_ns: fill.timestamp_ns,
                event_id: format!("event-{}", fill.sequence),
                order_id: fill.order_id.clone(),
                action_id: action_ids.get(&fill.order_id).cloned(),
                risk_decision_id: None,
                position_transition_id: None,
                equity_snapshot_id: None,
                symbol: fill.symbol.clone(),
                side: fill.side.clone(),
                quantity: fill.quantity.clone(),
                price: fill.price.clone(),
                fee: fill.fee.clone(),
                liquidity: None,
            })
            .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
    }
    let initial_cash = ledger_decimal(&configuration.initial_cash, "initial_cash")?;
    // Reuse canonical trade slices once; tick-level valuation history must not
    // scan the entire trade table for every mark. Equal exit sequences share
    // one causal fill boundary and partition_point includes all their slices.
    let mut ordered_trades: Vec<_> = ledger.trades.rows.iter().collect();
    ordered_trades.sort_by_key(|trade| trade.exit_sequence);
    let mut realized_prefix = Vec::with_capacity(ordered_trades.len());
    let mut cumulative_net = Decimal18::ZERO;
    for trade in ordered_trades {
        cumulative_net = overflow_ledger(
            cumulative_net.checked_add(trade.net_pnl),
            "snapshot realized PnL",
        )?;
        realized_prefix.push((trade.exit_sequence, cumulative_net));
    }

    let mut recorded = 0u64;
    let mut last_report = std::time::Instant::now();
    for row in ledger
        .rows
        .iter()
        .map_err(|e| BacktestJobError::Io(e.to_string()))?
    {
        let row = row.map_err(|e| BacktestJobError::Io(e.to_string()))?;
        let total_pnl =
            overflow_ledger(row.equity.checked_sub(initial_cash), "snapshot total PnL")?;
        let last_sequence = if row.fill_count == 0 {
            None
        } else {
            fills
                .get(row.fill_count as usize - 1)
                .map(|fill| fill.sequence)
        };
        let closed_count = last_sequence.map_or(0, |sequence| {
            realized_prefix.partition_point(|(exit_sequence, _)| *exit_sequence <= sequence)
        });
        let realized_pnl = if closed_count == 0 {
            Decimal18::ZERO
        } else {
            realized_prefix[closed_count - 1].1
        };
        let unrealized_pnl = overflow_ledger(
            total_pnl.checked_sub(realized_pnl),
            "snapshot unrealized PnL",
        )?;
        recorder
            .record_portfolio_equity(&PortfolioEquityRecord {
                timestamp_ns: row.timestamp_ns,
                cash: row.cash,
                equity: row.equity,
                market_value: row.market_value,
                realized_pnl,
                unrealized_pnl,
                total_pnl,
                position_count: row.position_count,
                fill_count: row.fill_count,
            })
            .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
        recorded += 1;
        if recorded.is_multiple_of(8192) && last_report.elapsed().as_secs() >= 1 {
            report_results(reporter, ExecutionStage::WritingArtifacts, recorded, false)?;
            last_report = std::time::Instant::now();
        }
    }
    for (name, value) in custom_metrics {
        recorder
            .record_emission(
                name,
                &JsonValue::object([
                    ("kind", JsonValue::string("custom_metric")),
                    ("value", JsonValue::String(value.clone())),
                ]),
                None,
                None,
                Some(configuration.end_ns),
            )
            .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
    }
    recorder
        .close()
        .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
    Ok(ledger.equity_metrics)
}
