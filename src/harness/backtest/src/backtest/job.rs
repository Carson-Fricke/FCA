//! End-to-end backtest job on the Rust stack.
//!
//! Mirrors the `backtest-job` console script: read a configuration document,
//! require the `backtest` shell, run one deterministic backtest through the
//! Rust kernels, and write the complete artifact set — the canonical
//! `backtest.json` document, `metrics.json`, the offline `report.html`, and the
//! results recorder's linked Parquet artifact trail plus its run manifest.
//!
//! [`BacktestJobRunner`] is the seam the `harness-api` binary calls: it owns
//! the optional output directory and runs one job against a configuration
//! path. The integration crate adapts it behind its own command surface.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use num::Decimal18;

use crate::backtest::config::BacktestConfig;
use crate::backtest::engine::{
    run_backtest, ApproveAll, BacktestOptions, ExecutionModel, ImmediateFill, OrderSide, RiskModel,
    StrategyRuntime, TradeBuyStrategy,
};
use crate::backtest::engine_core::{
    build_default_manifest, run_native_engine, BacktestOutcome, EngineOptions, StrategyInstance,
};
use crate::backtest::errors::{BacktestError, ONE_DAY_NS};
use crate::backtest::fixture::{synthetic_fixture, SyntheticDataOptions};
use crate::backtest::manifest::BacktestResult;
use crate::backtest::preflight::preflight_data;
use crate::canonical_json::JsonValue;
use crate::results::metrics::{
    equity_curve_metrics, execution_stats, trade_stats, EquityPoint, FillRow, OrderRow,
};
use crate::results::recorder::{
    ActionKind, ActionRecord, EquityRecord, FillRecord, RecorderConfig, RunManifest, RunRecorder,
};
use crate::results::report::{render_report, ReportHeader};

/// A backtest job failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BacktestJobError {
    /// The configuration document could not be read.
    Io(String),
    /// The document does not carry a `shell` key.
    MissingShell,
    /// The shell is not `backtest`.
    WrongShell(String),
    /// The engine rejected the run.
    Engine(String),
    /// An artifact could not be written.
    Artifact(String),
}

impl std::fmt::Display for BacktestJobError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(message) => write!(formatter, "{message}"),
            Self::MissingShell => formatter.write_str("configuration has no 'shell' key"),
            Self::WrongShell(shell) => {
                write!(
                    formatter,
                    "configuration shell must be 'backtest', not '{shell}'"
                )
            }
            Self::Engine(message) => write!(formatter, "{message}"),
            Self::Artifact(message) => write!(formatter, "{message}"),
        }
    }
}

impl std::error::Error for BacktestJobError {}

impl From<BacktestError> for BacktestJobError {
    fn from(error: BacktestError) -> Self {
        Self::Engine(error.to_string())
    }
}

/// The canonical summary the job prints after a run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BacktestJobSummary {
    /// Run identifier.
    pub run_id: String,
    /// Terminal status (`completed` or `aborted`).
    pub status: String,
    /// Canonical manifest hash.
    pub manifest_hash: String,
    /// Artifact digest.
    pub artifact_digest: String,
    /// Outcome digest.
    pub outcome_digest: String,
    /// Number of market events consumed.
    pub market_events: u64,
    /// Number of orders submitted.
    pub orders: u64,
    /// Number of fills produced.
    pub fills: u64,
    /// Path of the written `backtest.json` artifact.
    pub artifact_path: PathBuf,
    /// Path of the written `metrics.json` artifact.
    pub metrics_path: PathBuf,
    /// Path of the written `report.html` artifact.
    pub report_path: PathBuf,
    /// Directory holding the recorder's linked artifact trail.
    pub artifact_dir: PathBuf,
    /// Recorder run digest over the linked artifact trail.
    pub recorder_digest: String,
    /// Failure reason, present when the run did not complete.
    pub reason: Option<String>,
}

impl BacktestJobSummary {
    /// Render the summary exactly as the CLI writes it to stdout.
    #[must_use]
    pub fn stdout_lines(&self) -> Vec<String> {
        vec![
            format!("run_id: {}", self.run_id),
            format!("status: {}", self.status),
            format!("manifest_hash: {}", self.manifest_hash),
            format!("artifact_digest: {}", self.artifact_digest),
            format!("outcome_digest: {}", self.outcome_digest),
            format!("market_events: {}", self.market_events),
            format!("orders: {}", self.orders),
            format!("fills: {}", self.fills),
            format!("artifacts: {}", self.artifact_path.display()),
        ]
    }

    /// Every artifact path written for this run, in canonical order.
    #[must_use]
    pub fn artifact_paths(&self) -> Vec<PathBuf> {
        vec![
            self.artifact_path.clone(),
            self.metrics_path.clone(),
            self.report_path.clone(),
            self.artifact_dir.clone(),
        ]
    }
}

/// Read the top-level `shell` scalar from a configuration document.
#[must_use]
pub fn parse_shell(text: &str) -> Option<String> {
    for line in text.lines() {
        let trimmed = line.trim_end();
        if trimmed.trim_start().len() != trimmed.len() {
            continue;
        }
        let rest = trimmed
            .strip_prefix("shell:")
            .or_else(|| trimmed.strip_prefix("\"shell\":"));
        if let Some(rest) = rest {
            let value = rest
                .trim()
                .trim_matches(|character| character == '"' || character == '\'');
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

/// Read a top-level scalar (string or integer) from a configuration document.
#[must_use]
pub fn parse_scalar(text: &str, key: &str) -> Option<String> {
    for line in text.lines() {
        let trimmed = line.trim_end();
        if trimmed.trim_start().len() != trimmed.len() {
            continue;
        }
        let rest = trimmed
            .strip_prefix(&format!("{key}:"))
            .or_else(|| trimmed.strip_prefix(&format!("\"{key}\":")));
        if let Some(rest) = rest {
            let value = rest
                .trim()
                .trim_matches(|character| character == '"' || character == '\'');
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

/// Read a scalar one indent level below a top-level `section:` block.
#[must_use]
pub fn parse_nested_scalar(text: &str, section: &str, key: &str) -> Option<String> {
    let mut in_section = false;
    for line in text.lines() {
        let trimmed = line.trim_end();
        if trimmed.trim().is_empty() || trimmed.trim_start().starts_with('#') {
            continue;
        }
        let indented = trimmed.len() != trimmed.trim_start().len();
        if !indented {
            in_section = trimmed.trim_end() == format!("{section}:");
            continue;
        }
        if !in_section {
            continue;
        }
        let content = trimmed.trim();
        let rest = content
            .strip_prefix(&format!("{key}:"))
            .or_else(|| content.strip_prefix(&format!("\"{key}\":")));
        if let Some(rest) = rest {
            let value = rest
                .trim()
                .trim_matches(|character| character == '"' || character == '\'');
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

/// Parse an ISO-8601 UTC timestamp into epoch nanoseconds.
///
/// Accepts the `YYYY-MM-DDThh:mm:ss[.ffffff]Z` / `+00:00` form the Python
/// `harness.configuration` loader resolves into a UTC `datetime`.
#[must_use]
pub fn parse_timestamp_ns(value: &str) -> Option<i64> {
    let text = value.trim();
    let (date, time) = text.split_once('T')?;
    let mut date_parts = date.split('-');
    let year: i64 = date_parts.next()?.parse().ok()?;
    let month: i64 = date_parts.next()?.parse().ok()?;
    let day: i64 = date_parts.next()?.parse().ok()?;
    let time = time.trim_end_matches('Z');
    let time = time.split_once('+').map_or(time, |(left, _)| left);
    let mut time_parts = time.split(':');
    let hour: i64 = time_parts.next()?.parse().ok()?;
    let minute: i64 = time_parts.next()?.parse().ok()?;
    let second: i64 = time_parts
        .next()
        .and_then(|part| part.split('.').next())
        .and_then(|part| part.parse().ok())
        .unwrap_or(0);
    let days = crate::backtest::fixture::days_from_civil(year, month, day);
    let seconds = ((days * 24 + hour) * 60 + minute) * 60 + second;
    Some(seconds * 1_000_000_000)
}

/// The default synthetic run window: 2024-01-01 to 2024-01-03 UTC.
pub(crate) const DEFAULT_START_NS: i64 = 19_723 * ONE_DAY_NS;
pub(crate) const DEFAULT_END_NS: i64 = 19_725 * ONE_DAY_NS;

/// Run one backtest job using the built-in deterministic strategy seam.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the configuration is missing or invalid,
/// the engine rejects the run, or an artifact cannot be written.
pub fn run_backtest_job(
    config_path: &Path,
    output_dir: Option<&Path>,
) -> Result<BacktestJobSummary, BacktestJobError> {
    let configuration = load_backtest_config(config_path)?;
    let options = BacktestOptions::default();
    run_job_with(
        &configuration,
        &options,
        output_dir,
        &mut TradeBuyStrategy::default(),
        &mut ApproveAll,
        &mut ImmediateFill,
    )
}

/// Load and validate the resolved fields one backtest job reads.
///
/// The full Python loader in `harness.configuration` validates the whole schema
/// graph; this reads the same typed fields the staged runner consumes: the
/// shell, run window, seed, accounting settings, and data identity.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the document is missing a `backtest`
/// shell, carries an invalid window, or cannot be read.
pub fn load_backtest_config(config_path: &Path) -> Result<BacktestConfig, BacktestJobError> {
    let text = std::fs::read_to_string(config_path)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let shell = parse_shell(&text).ok_or(BacktestJobError::MissingShell)?;
    if shell != "backtest" {
        return Err(BacktestJobError::WrongShell(shell));
    }
    BacktestConfig::from_yaml(&text).map_err(BacktestJobError::from)
}

/// The seam the `harness-api` binary calls to run one backtest job.
#[derive(Clone, Debug, Default)]
pub struct BacktestJobRunner {
    output_dir: Option<PathBuf>,
}

impl BacktestJobRunner {
    /// Create a runner using the default output directory.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Create a runner writing artifacts under `output_dir`.
    #[must_use]
    pub fn with_output_dir(output_dir: PathBuf) -> Self {
        Self {
            output_dir: Some(output_dir),
        }
    }

    /// The configured output directory, if any.
    #[must_use]
    pub fn output_dir(&self) -> Option<&Path> {
        self.output_dir.as_deref()
    }

    /// Run one backtest job for a configuration document.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestJobError`] under the same conditions as
    /// [`run_backtest_job`].
    pub fn run(&self, config_path: &Path) -> Result<BacktestJobSummary, BacktestJobError> {
        run_backtest_job(config_path, self.output_dir.as_deref())
    }
}

/// Run one backtest job with injected strategy, risk, and execution seams.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the engine rejects the run or an artifact
/// cannot be written.
pub fn run_job_with(
    configuration: &BacktestConfig,
    options: &BacktestOptions,
    output_dir: Option<&Path>,
    strategy: &mut dyn StrategyRuntime,
    risk: &mut dyn RiskModel,
    execution: &mut dyn ExecutionModel,
) -> Result<BacktestJobSummary, BacktestJobError> {
    let result = run_backtest(configuration, None, options, strategy, risk, execution)?;
    let directory = output_dir.map_or_else(
        || {
            PathBuf::from(".local")
                .join("backtests")
                .join(&configuration.run_id)
        },
        Path::to_path_buf,
    );
    std::fs::create_dir_all(&directory).map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let artifact_path = write_artifact(&directory, &configuration.run_id, &result)?;
    let ledger = build_ledger(configuration, &result);
    let metrics_path = write_metrics(&directory, &result, &ledger)?;
    let report_path = write_report(
        &directory,
        &configuration.run_id,
        configuration.seed,
        &ledger,
    )?;
    let artifact_dir = directory.join("artifacts");
    let recorder = record_artifacts(&artifact_dir, configuration, &result, &ledger)?;
    Ok(summarize(
        &result,
        artifact_path,
        metrics_path,
        report_path,
        artifact_dir,
        recorder,
    ))
}

/// Run one native engine job with injected Python-backed strategy runtimes.
///
/// The engine core, fee schedule, venue, portfolio, and risk pipeline are all
/// native Rust; the strategies stay Python and are driven through
/// [`StrategyInstance`]. Writes the canonical `backtest.json` artifact and
/// returns the outcome with its path.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the configuration, fixture, engine, or
/// artifact write fails.
pub fn run_native_job(
    configuration: &BacktestConfig,
    engine_options: &EngineOptions,
    output_dir: Option<&Path>,
    strategies: Vec<StrategyInstance>,
) -> Result<(BacktestOutcome, PathBuf), BacktestJobError> {
    let synthetic_options = SyntheticDataOptions::default();
    let fixture = synthetic_fixture(configuration, &synthetic_options)?;
    let preflight = preflight_data(configuration, &fixture)?;
    let manifest = build_default_manifest(configuration, &fixture, &preflight)?;
    let outcome = run_native_engine(
        configuration,
        &fixture,
        &preflight,
        manifest,
        engine_options,
        strategies,
        &synthetic_options,
    )?;
    let directory = output_dir.map_or_else(
        || {
            PathBuf::from(".local")
                .join("backtests")
                .join(&configuration.run_id)
        },
        Path::to_path_buf,
    );
    std::fs::create_dir_all(&directory).map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let artifact_path = directory.join("backtest.json");
    let mut payload = outcome.artifact_json().into_bytes();
    payload.push(b'\n');
    std::fs::write(&artifact_path, payload)
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    Ok((outcome, artifact_path))
}

fn write_artifact(
    directory: &Path,
    run_id: &str,
    result: &BacktestResult,
) -> Result<PathBuf, BacktestJobError> {
    let path = directory.join("backtest.json");
    let mut payload = result.artifact_json().into_bytes();
    payload.push(b'\n');
    // The run identifier is embedded in the manifest, so the written document
    // is self-describing and independent of the directory name.
    debug_assert_eq!(result.manifest.run_id, run_id);
    std::fs::write(&path, payload)
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    Ok(path)
}

/// One point of the deterministic job ledger derived from the run's fills.
struct LedgerRow {
    timestamp_ns: i64,
    cash: Decimal18,
    equity: Decimal18,
    market_value: Decimal18,
    position_count: u32,
    fill_count: u64,
}

/// The deterministic metrics the job derives from the engine's fills.
struct JobLedger {
    equity: Vec<EquityPoint>,
    rows: Vec<LedgerRow>,
    fills: Vec<FillRow>,
    orders: Vec<OrderRow>,
}

fn parse_dec(value: &str) -> Decimal18 {
    value.parse::<Decimal18>().unwrap_or(Decimal18::ZERO)
}

fn build_ledger(configuration: &BacktestConfig, result: &BacktestResult) -> JobLedger {
    let mut cash = parse_dec(&configuration.initial_cash);
    let mut positions: BTreeMap<String, Decimal18> = BTreeMap::new();
    let mut marks: BTreeMap<String, Decimal18> = BTreeMap::new();
    let mut equity = vec![EquityPoint {
        timestamp_ns: configuration.start_ns,
        equity: cash,
    }];
    let mut rows = vec![LedgerRow {
        timestamp_ns: configuration.start_ns,
        cash,
        equity: cash,
        market_value: Decimal18::ZERO,
        position_count: 0,
        fill_count: 0,
    }];
    let mut fills = Vec::new();
    for (index, fill) in result.fills.iter().enumerate() {
        let fill_count = index as u64 + 1;
        let quantity = parse_dec(&fill.quantity);
        let price = parse_dec(&fill.price);
        let fee = parse_dec(&fill.fee);
        let notional = quantity.checked_mul(price).unwrap_or(Decimal18::ZERO);
        let position = positions
            .entry(fill.symbol.clone())
            .or_insert(Decimal18::ZERO);
        match fill.side {
            OrderSide::Buy => {
                cash = cash
                    .checked_sub(notional)
                    .and_then(|value| value.checked_sub(fee))
                    .unwrap_or(cash);
                *position = position.checked_add(quantity).unwrap_or(*position);
            }
            OrderSide::Sell => {
                cash = cash
                    .checked_add(notional)
                    .and_then(|value| value.checked_sub(fee))
                    .unwrap_or(cash);
                *position = position.checked_sub(quantity).unwrap_or(*position);
            }
        }
        marks.insert(fill.symbol.clone(), price);
        let mut market_value = Decimal18::ZERO;
        for (symbol, held) in &positions {
            if let Some(mark) = marks.get(symbol) {
                market_value = market_value
                    .checked_add(held.checked_mul(*mark).unwrap_or(Decimal18::ZERO))
                    .unwrap_or(market_value);
            }
        }
        let equity_value = cash.checked_add(market_value).unwrap_or(cash);
        equity.push(EquityPoint {
            timestamp_ns: fill.timestamp_ns,
            equity: equity_value,
        });
        rows.push(LedgerRow {
            timestamp_ns: fill.timestamp_ns,
            cash,
            equity: equity_value,
            market_value,
            position_count: positions.values().filter(|held| !held.is_zero()).count() as u32,
            fill_count,
        });
        fills.push(FillRow {
            fill_id: fill.fill_id.clone(),
            order_id: fill.order_id.clone(),
            strategy_id: String::new(),
            quantity,
            price,
            fee,
            slippage: Decimal18::ZERO,
            latency_ns: None,
        });
    }
    let orders = result
        .orders
        .iter()
        .map(|order| OrderRow {
            order_id: order.order_id.clone(),
            strategy_id: String::new(),
            outcome: "filled".to_string(),
        })
        .collect();
    JobLedger {
        equity,
        rows,
        fills,
        orders,
    }
}

fn decimal_opt(value: Option<Decimal18>) -> JsonValue {
    match value {
        Some(value) => JsonValue::String(value.to_string()),
        None => JsonValue::Null,
    }
}

fn metrics_json(result: &BacktestResult, ledger: &JobLedger) -> JsonValue {
    let metrics = equity_curve_metrics(&ledger.equity);
    let execution = execution_stats(&ledger.fills, &ledger.orders);
    JsonValue::object([
        ("run_id", JsonValue::String(result.manifest.run_id.clone())),
        ("status", JsonValue::string(result.status.value())),
        (
            "counts",
            JsonValue::object([
                ("market_events", JsonValue::UInt(result.market_event_count)),
                ("dispatched", JsonValue::UInt(result.dispatched_count)),
                ("orders", JsonValue::UInt(result.order_count)),
                ("fills", JsonValue::UInt(result.fill_count)),
            ]),
        ),
        (
            "equity",
            JsonValue::object([
                ("samples", JsonValue::UInt(ledger.equity.len() as u64)),
                ("total_return", decimal_opt(metrics.total_return)),
                (
                    "cagr",
                    match metrics.cagr {
                        Some(value) => JsonValue::String(format!("{value:.10}")),
                        None => JsonValue::Null,
                    },
                ),
                (
                    "max_drawdown",
                    JsonValue::String(metrics.max_drawdown.to_string()),
                ),
                (
                    "drawdown_duration_ns",
                    JsonValue::Int(metrics.drawdown.duration_ns),
                ),
                (
                    "drawdown_recovered",
                    JsonValue::Bool(metrics.drawdown.recovered),
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
                (
                    "slippage_total",
                    JsonValue::String(execution.slippage_total.to_string()),
                ),
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
    ])
}

fn write_metrics(
    directory: &Path,
    result: &BacktestResult,
    ledger: &JobLedger,
) -> Result<PathBuf, BacktestJobError> {
    let path = directory.join("metrics.json");
    let mut payload = metrics_json(result, ledger)
        .to_canonical_string()
        .into_bytes();
    payload.push(b'\n');
    std::fs::write(&path, payload)
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    Ok(path)
}

fn write_report(
    directory: &Path,
    run_id: &str,
    seed: u64,
    ledger: &JobLedger,
) -> Result<PathBuf, BacktestJobError> {
    let mut header = ReportHeader::new(run_id);
    header.seed = seed.to_string();
    let trades = trade_stats(&[]);
    let document = render_report(&header, &ledger.equity, &trades);
    let path = directory.join("report.html");
    std::fs::write(&path, document.into_bytes())
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    Ok(path)
}

fn record_artifacts(
    directory: &Path,
    configuration: &BacktestConfig,
    result: &BacktestResult,
    ledger: &JobLedger,
) -> Result<RunManifest, BacktestJobError> {
    let config = RecorderConfig::new(&configuration.run_id, directory)
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?
        .with_seed(configuration.seed as i64)
        .with_account_id("backtest")
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    let mut recorder =
        RunRecorder::new(config).map_err(|error| BacktestJobError::Engine(error.to_string()))?;
    let mut action_ids: BTreeMap<String, String> = BTreeMap::new();
    for (index, order) in result.orders.iter().enumerate() {
        let action_id = recorder
            .record_action(&ActionRecord {
                kind: Some(ActionKind::Submit),
                symbol: Some(order.symbol.clone()),
                side: Some(order.side.value().to_string()),
                quantity: Some(order.quantity.clone()),
                order_id: Some(order.order_id.clone()),
                at_ns: Some(configuration.start_ns + index as i64),
                ..ActionRecord::default()
            })
            .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
        action_ids.insert(order.order_id.clone(), action_id);
    }
    for fill in &result.fills {
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
                side: fill.side.value().to_string(),
                quantity: fill.quantity.clone(),
                price: fill.price.clone(),
                fee: fill.fee.clone(),
                liquidity: None,
            })
            .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
    }
    for row in &ledger.rows {
        recorder
            .record_equity(&EquityRecord {
                source: "portfolio".to_string(),
                account_id: None,
                book_id: None,
                cash: row.cash.to_string(),
                equity: Some(row.equity.to_string()),
                market_value: Some(row.market_value.to_string()),
                realized_pnl: "0".to_string(),
                unrealized_pnl: None,
                total_pnl: Some(row.equity.to_string()),
                position_count: row.position_count,
                fill_count: row.fill_count,
                at_ns: Some(row.timestamp_ns),
                snapshot_id: None,
            })
            .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
    }
    recorder
        .close()
        .map_err(|error| BacktestJobError::Engine(error.to_string()))
}

fn summarize(
    result: &BacktestResult,
    artifact_path: PathBuf,
    metrics_path: PathBuf,
    report_path: PathBuf,
    artifact_dir: PathBuf,
    recorder: RunManifest,
) -> BacktestJobSummary {
    BacktestJobSummary {
        run_id: result.manifest.run_id.clone(),
        status: result.status.value().to_string(),
        manifest_hash: result.manifest.manifest_hash(),
        artifact_digest: result.artifact_digest(),
        outcome_digest: result.outcome_digest(),
        market_events: result.market_event_count,
        orders: result.order_count,
        fills: result.fill_count,
        artifact_path,
        metrics_path,
        report_path,
        artifact_dir,
        recorder_digest: recorder.run_digest,
        reason: result.reason.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "harness-backtest-job-{}-{name}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|value| value.as_nanos())
                .unwrap_or(0)
        ))
    }

    #[test]
    fn parses_shell_and_scalars() {
        assert_eq!(
            parse_shell("shell: backtest\n").as_deref(),
            Some("backtest")
        );
        assert_eq!(parse_shell("market_data:\n  shell: nested\n"), None);
        assert_eq!(
            parse_scalar("run_id: bt-1\n", "run_id").as_deref(),
            Some("bt-1")
        );
        assert_eq!(
            parse_scalar("\"seed\": 42\n", "seed").as_deref(),
            Some("42")
        );
    }

    #[test]
    fn parses_nested_section_scalars() {
        let text = "shell: backtest\ndata:\n  snapshot_id: lake-v1\n  fingerprint: bbbb\n  symbols: [SPY]\nseed: 1\n";
        assert_eq!(
            parse_nested_scalar(text, "data", "snapshot_id").as_deref(),
            Some("lake-v1")
        );
        assert_eq!(
            parse_nested_scalar(text, "data", "fingerprint").as_deref(),
            Some("bbbb")
        );
        assert_eq!(parse_nested_scalar(text, "data", "absent"), None);
        assert_eq!(parse_nested_scalar(text, "other", "snapshot_id"), None);
    }

    #[test]
    fn parses_iso_timestamps_to_epoch_nanoseconds() {
        assert_eq!(
            parse_timestamp_ns("2024-01-02T14:30:00Z"),
            Some(
                crate::backtest::fixture::days_from_civil(2024, 1, 2) * ONE_DAY_NS
                    + 14 * 3_600_000_000_000
                    + 30 * 60_000_000_000
            )
        );
        assert_eq!(
            parse_timestamp_ns("2024-01-02T14:30:00+00:00"),
            parse_timestamp_ns("2024-01-02T14:30:00Z")
        );
        assert_eq!(parse_timestamp_ns("not-a-date"), None);
    }

    #[test]
    fn rejects_wrong_shell_and_missing_config() {
        let directory = temp_dir("reject");
        std::fs::create_dir_all(&directory).expect("dir");
        let api = directory.join("api.yaml");
        std::fs::write(&api, "shell: api\n").expect("write");
        assert_eq!(
            run_backtest_job(&api, None),
            Err(BacktestJobError::WrongShell("api".to_string()))
        );
        assert!(matches!(
            run_backtest_job(&directory.join("missing.yaml"), None),
            Err(BacktestJobError::Io(_))
        ));
        let _ = std::fs::remove_dir_all(&directory);
    }

    #[test]
    fn runs_end_to_end_and_writes_the_artifact_set() {
        let directory = temp_dir("run");
        std::fs::create_dir_all(&directory).expect("dir");
        let config = directory.join("backtest.yaml");
        std::fs::write(&config, "shell: backtest\nrun_id: bt-e2e\nseed: 7\n").expect("write");
        let output = directory.join("out");
        let summary = run_backtest_job(&config, Some(&output)).expect("job");
        assert_eq!(summary.run_id, "bt-e2e");
        assert_eq!(summary.status, "completed");
        assert_eq!(summary.market_events, 36);
        assert!(summary.artifact_path.exists());
        let document = std::fs::read_to_string(&summary.artifact_path).expect("read");
        assert!(document.contains("\"manifest\""));
        assert!(document.contains("\"counts\""));
        assert_eq!(summary.artifact_digest.len(), 64);
        // The summary renders the CLI lines deterministically.
        assert_eq!(summary.stdout_lines().len(), 9);
        // The complete end-to-end artifact set is present and self-contained.
        assert!(summary.metrics_path.exists());
        assert!(summary.report_path.exists());
        assert!(summary.artifact_dir.join("manifest.parquet").exists());
        let metrics = std::fs::read_to_string(&summary.metrics_path).expect("metrics");
        assert!(metrics.contains("\"equity\""));
        assert!(metrics.contains("\"execution\""));
        let report = std::fs::read_to_string(&summary.report_path).expect("report");
        assert!(report.starts_with("<!doctype html>"));
        assert_eq!(summary.recorder_digest.len(), 64);
        assert_eq!(summary.artifact_paths().len(), 4);
        let _ = std::fs::remove_dir_all(&directory);
    }

    #[test]
    fn runner_seam_runs_a_job_with_a_configured_output_dir() {
        let directory = temp_dir("seam");
        std::fs::create_dir_all(&directory).expect("dir");
        let config = directory.join("backtest.yaml");
        std::fs::write(&config, "shell: backtest\nrun_id: bt-seam\nseed: 3\n").expect("write");
        let output = directory.join("out");
        let runner = BacktestJobRunner::with_output_dir(output.clone());
        assert_eq!(runner.output_dir(), Some(output.as_path()));
        let summary = runner.run(&config).expect("job");
        assert_eq!(summary.run_id, "bt-seam");
        assert!(summary.report_path.starts_with(&output));
        let _ = std::fs::remove_dir_all(&directory);
    }
}
