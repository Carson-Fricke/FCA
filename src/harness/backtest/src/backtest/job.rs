//! End-to-end backtest job on the Rust stack.
//!
//! Mirrors the `backtest-job` console script: read a configuration document,
//! require the `backtest` shell, ensure and stream the run window's market data
//! from the market-lake routes, run one deterministic backtest through the Rust
//! kernels, and write the complete artifact set — the canonical
//! `backtest.json` document, `metrics.json`, the offline `report.html`, and the
//! results recorder's linked Parquet artifact trail plus its run manifest.
//!
//! The routes are read through the injected [`BlockingLakeClient`]. There is no
//! data-directory parameter and no synthetic fallback: a run whose lake cannot
//! serve the window fails loudly.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use harness_lakeclient::{BlockingLakeClient, EnsureSpec, ReadSpec};
use num::Decimal18;

use marketdata::{DataType, Date};

use crate::backtest::config::BacktestConfig;
use crate::backtest::engine_core::{
    build_default_manifest, normalize_subscriptions, run_native_engine, runtime_subscriptions,
    BacktestOutcome, EngineOptions, MarketSubscription, StrategyInstance,
};
use crate::backtest::errors::{BacktestError, ONE_DAY_NS};
use crate::backtest::fixture::{
    ExchangeSession, MarketEvent, ReplayFingerprints, ReplayFixture, SyntheticDataOptions,
};
use crate::backtest::preflight::preflight_data;
use crate::backtest::screen::ScreenConfig;
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
    /// The shell is not `screen`.
    WrongScreenShell(String),
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
            Self::WrongScreenShell(shell) => {
                write!(
                    formatter,
                    "configuration shell must be 'screen', not '{shell}'"
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

/// Read the top-level `shell` scalar from a configuration document.
#[must_use]
pub fn parse_shell(text: &str) -> Option<String> {
    let mapping = config::replay::parse_document(text).ok()?;
    match mapping.get("shell") {
        Some(config::YamlValue::Str(shell)) if !shell.is_empty() => Some(shell.clone()),
        _ => None,
    }
}

/// Read a top-level scalar (string or integer) from a configuration document.
#[must_use]
pub fn parse_scalar(text: &str, key: &str) -> Option<String> {
    let mapping = config::replay::parse_document(text).ok()?;
    match mapping.get(key)? {
        config::YamlValue::Str(v) if !v.is_empty() => Some(v.clone()),
        config::YamlValue::Int(v) => Some(v.to_string()),
        _ => None,
    }
}

/// Read a scalar one indent level below a top-level `section:` block.
#[must_use]
pub fn parse_nested_scalar(text: &str, section: &str, key: &str) -> Option<String> {
    let mapping = config::replay::parse_document(text).ok()?;
    let config::YamlValue::Map(section) = mapping.get(section)? else {
        return None;
    };
    match section.get(key)? {
        config::YamlValue::Str(v) if !v.is_empty() => Some(v.clone()),
        config::YamlValue::Int(v) => Some(v.to_string()),
        _ => None,
    }
}

/// Parse an RFC3339 timestamp with explicit offset at exact nanosecond precision.
/// Returns `None` for malformed or out-of-range bounds rather than rounding.
#[must_use]
pub fn parse_timestamp_ns(value: &str) -> Option<i64> {
    let text = value.trim();
    if !text.is_ascii() || text.len() < 20 {
        return None;
    }
    let digits = |part: &str| -> Option<i64> {
        if part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        part.parse().ok()
    };
    if !matches!(text.as_bytes()[10], b'T' | b't') || &text[4..5] != "-" || &text[7..8] != "-" {
        return None;
    }
    digits(&text[..4])?;
    digits(&text[5..7])?;
    digits(&text[8..10])?;
    let date = Date::parse(&text[..10])?;
    let time = &text[11..];
    let (clock, offset) = if time.ends_with(['Z', 'z']) {
        (&time[..time.len() - 1], 0)
    } else {
        let index = time.rfind(['+', '-'])?;
        let zone = &time[index..];
        if zone.len() != 6 || &zone[3..4] != ":" {
            return None;
        }
        let hours = digits(&zone[1..3])?;
        let minutes = digits(&zone[4..6])?;
        if hours > 23 || minutes > 59 {
            return None;
        }
        let sign = if zone.starts_with('+') { 1 } else { -1 };
        (&time[..index], sign * (hours * 3600 + minutes * 60))
    };
    let (clock, fraction) = match clock.split_once('.') {
        Some((clock, fraction)) => {
            if fraction.len() > 9 {
                return None;
            }
            let nanos = digits(fraction)? * 10_i64.pow(9 - fraction.len() as u32);
            (clock, nanos)
        }
        None => (clock, 0),
    };
    if clock.len() != 8 || &clock[2..3] != ":" || &clock[5..6] != ":" {
        return None;
    }
    let hour = digits(&clock[..2])?;
    let minute = digits(&clock[3..5])?;
    let second = digits(&clock[6..8])?;
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let seconds =
        i128::from(date.days()) * 86_400 + i128::from(hour * 3600 + minute * 60 + second - offset);
    i64::try_from(seconds * 1_000_000_000 + i128::from(fraction)).ok()
}

/// The default synthetic run window: 2024-01-01 to 2024-01-03 UTC.
pub(crate) const DEFAULT_START_NS: i64 = 19_723 * ONE_DAY_NS;
pub(crate) const DEFAULT_END_NS: i64 = 19_725 * ONE_DAY_NS;

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
    resolve_authored_sources(
        BacktestConfig::from_yaml(&text).map_err(BacktestJobError::from)?,
        config_path,
    )
}

/// Load and validate one study configuration document (shell `study`).
///
/// A study reuses the replay configuration shape but declares the `study`
/// shell, so it is never confused with a backtest. The strategy-instance list
/// names platform study programs rather than user strategies, and the raw
/// document declares exact `data.subscriptions`. Tick-only inputs need no
/// interval.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the document is missing a `study` shell,
/// omits `data.subscriptions`, carries an invalid window, or cannot be read.
pub fn load_study_config(config_path: &Path) -> Result<BacktestConfig, BacktestJobError> {
    let text = std::fs::read_to_string(config_path)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let shell = parse_shell(&text).ok_or(BacktestJobError::MissingShell)?;
    if shell != "study" {
        return Err(BacktestJobError::WrongShell(shell));
    }
    resolve_authored_sources(
        BacktestConfig::from_yaml(&text).map_err(BacktestJobError::from)?,
        config_path,
    )
}

/// Load and validate one screening configuration document (shell `screen`).
///
/// The frozen wire shape carries the authored Rust source (or a
/// `source_path` resolved relative to the document) plus its `bundle_hash`,
/// and the definition provenance Thesis attaches to the run. A document whose
/// shell is not `screen`, whose keys are unknown, or whose `bundle_hash` does
/// not match the LF-normalized source fails before any JIT work starts.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the document is missing a `screen` shell,
/// carries an invalid or mismatched configuration, or cannot be read.
pub fn load_screen_config(config_path: &Path) -> Result<ScreenConfig, BacktestJobError> {
    let text = std::fs::read_to_string(config_path)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let shell = parse_shell(&text).ok_or(BacktestJobError::MissingShell)?;
    if shell != "screen" {
        return Err(BacktestJobError::WrongScreenShell(shell));
    }
    let base_dir = config_path.parent();
    if text.trim_start().starts_with('{') {
        let document: serde_json::Value =
            serde_json::from_str(&text).map_err(|error| BacktestJobError::Io(error.to_string()))?;
        return ScreenConfig::from_json_value_with_base(&document, base_dir)
            .map_err(BacktestJobError::from);
    }
    let value = crate::backtest::yaml::load_yaml(&text)
        .map_err(|error| BacktestJobError::from(BacktestError::Configuration(error.0)))?;
    ScreenConfig::from_yaml_value_with_base(&value, base_dir).map_err(BacktestJobError::from)
}

/// Parse one in-memory study configuration document (JSON tree, `study` shell).
///
/// The harness builds this document from an authored model before compiling
/// it, so parsing reuses the exact typed path [`load_study_config`] uses.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the document has no `study` shell or the
/// typed configuration does not parse.
pub fn parse_study_document(
    document: &serde_json::Value,
) -> Result<BacktestConfig, BacktestJobError> {
    let shell = document
        .get("shell")
        .and_then(serde_json::Value::as_str)
        .ok_or(BacktestJobError::MissingShell)?;
    if shell != "study" {
        return Err(BacktestJobError::WrongShell(shell.to_string()));
    }
    let configuration =
        BacktestConfig::from_yaml(&document.to_string()).map_err(BacktestJobError::from)?;
    Ok(configuration)
}

/// Inline `parameters.source_path` authored sources after parsing.
///
/// The repository's non-executable YAML subset cannot carry multi-line
/// authored sources, so a YAML configuration references the authored file by
/// path, resolved relative to the configuration document's directory. JSON
/// documents may carry the source inline and need no resolution.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when a `source_path` parameter is not a
/// string or the referenced file cannot be read.
fn resolve_authored_sources(
    mut configuration: BacktestConfig,
    config_path: &Path,
) -> Result<BacktestConfig, BacktestJobError> {
    for instance in &mut configuration.strategy_instances {
        if instance.parameters.contains_key("source") {
            continue;
        }
        let Some(JsonValue::String(relative)) = instance.parameters.get("source_path") else {
            continue;
        };
        let resolved = config_path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(relative.as_str());
        let source = std::fs::read_to_string(&resolved).map_err(|error| {
            BacktestJobError::Io(format!(
                "strategy instance {:?} authored source {} could not be read: {error}",
                instance.instance_id,
                resolved.display()
            ))
        })?;
        instance
            .parameters
            .insert("source".to_string(), JsonValue::String(source));
    }
    Ok(configuration)
}

/// Run one native engine job over the market-lake routes.
///
/// The engine core, fee schedule, venue, portfolio, and risk pipeline are all
/// native Rust; the strategies are JIT-authored Rust programs driven through
/// [`StrategyInstance`]. Writes the canonical `backtest.json` artifact and
/// returns the outcome with its path.
///
/// The run window's quote and trade data is ensured and streamed through the
/// injected [`BlockingLakeClient`]; a lake that cannot serve the configured
/// window fails the job. A run without a configured lake endpoint never reaches
/// this function: the client cannot be built.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the configuration, data source, engine, or
/// artifact write fails.
#[allow(clippy::too_many_arguments)]
pub fn run_native_job_with_lake_client(
    configuration: &BacktestConfig,
    engine_options: &EngineOptions,
    output_dir: Option<&Path>,
    strategies: Vec<StrategyInstance>,
    client: &BlockingLakeClient,
) -> Result<(BacktestOutcome, PathBuf), BacktestJobError> {
    let subscriptions = runtime_subscriptions(configuration, &strategies)?;
    let fixture = lake_fixture_with_subscriptions(configuration, client, &subscriptions)?;
    let outcome = run_native_engine_outcome(
        configuration,
        engine_options,
        strategies,
        fixture,
        &lake_scale_options(),
    )?;
    let artifact_path = write_run_artifacts(
        configuration,
        &outcome,
        output_dir,
        "backtests",
        "backtest.json",
    )?;
    Ok((outcome, artifact_path))
}

/// Run one native study job: the replay driven by study programs with no order
/// execution. Writes the canonical `study.json` artifact and returns the outcome
/// with its path.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the configuration, fixture, engine, or
/// artifact write fails.
pub fn run_native_study_job_with_lake_client(
    configuration: &BacktestConfig,
    engine_options: &EngineOptions,
    output_dir: Option<&Path>,
    programs: Vec<StrategyInstance>,
    client: &BlockingLakeClient,
) -> Result<(BacktestOutcome, PathBuf), BacktestJobError> {
    let subscriptions = runtime_subscriptions(configuration, &programs)?;
    let fixture = lake_fixture_with_subscriptions(configuration, client, &subscriptions)?;
    let outcome = run_native_engine_outcome(
        configuration,
        engine_options,
        programs,
        fixture,
        &lake_scale_options(),
    )?;
    let artifact_path =
        write_run_artifacts(configuration, &outcome, output_dir, "studies", "study.json")?;
    Ok((outcome, artifact_path))
}

/// Drive the native engine for one job over a lake fixture.
///
/// The fixture is load-bearing: the route reads that built it already failed
/// loudly when the lake could not serve the configured window, so a run never
/// reports fabricated results.
fn run_native_engine_outcome(
    configuration: &BacktestConfig,
    engine_options: &EngineOptions,
    strategies: Vec<StrategyInstance>,
    fixture: ReplayFixture,
    synthetic_options: &SyntheticDataOptions,
) -> Result<BacktestOutcome, BacktestJobError> {
    let preflight = preflight_data(configuration, &fixture)?;
    let manifest = build_default_manifest(configuration, &fixture, &preflight)?;
    Ok(run_native_engine(
        configuration,
        &fixture,
        &preflight,
        manifest,
        engine_options,
        strategies,
        synthetic_options,
    )?)
}

/// Write the canonical artifact and complete result set for one native outcome.
fn write_run_artifacts(
    configuration: &BacktestConfig,
    outcome: &BacktestOutcome,
    output_dir: Option<&Path>,
    default_subdir: &str,
    artifact_name: &str,
) -> Result<PathBuf, BacktestJobError> {
    let directory = output_dir.map_or_else(
        || {
            PathBuf::from(".local")
                .join(default_subdir)
                .join(&configuration.run_id)
        },
        Path::to_path_buf,
    );
    std::fs::create_dir_all(&directory).map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let artifact_path = directory.join(artifact_name);
    let mut payload = outcome.artifact_json().into_bytes();
    payload.push(b'\n');
    std::fs::write(&artifact_path, payload)
        .map_err(|error| BacktestJobError::Artifact(error.to_string()))?;
    write_native_results(&directory, configuration, outcome)?;
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
fn write_native_results(
    directory: &Path,
    configuration: &BacktestConfig,
    outcome: &BacktestOutcome,
) -> Result<(), BacktestJobError> {
    let fills = ledger_fills_from_outcome(outcome);
    let orders = ledger_orders_from_outcome(outcome);
    let ledger = build_ledger(configuration, &fills, &orders);
    let counts = JobCounts {
        market_events: outcome.market_event_count,
        dispatched: outcome.dispatched_count,
        bar_frames: (configuration.shell == "study").then_some(outcome.bar_frame_count),
        orders: outcome.order_count,
        fills: outcome.fills.len() as u64,
    };
    write_metrics(
        directory,
        &outcome.manifest.run_id,
        outcome.status.value(),
        &counts,
        &ledger,
        &outcome.custom_metrics,
    )?;
    write_report(
        directory,
        &outcome.manifest.run_id,
        configuration.seed,
        &ledger,
    )?;
    let artifact_dir = directory.join("artifacts");
    record_artifacts(
        &artifact_dir,
        configuration,
        &orders,
        &fills,
        &ledger,
        &outcome.custom_metrics,
    )?;
    Ok(())
}

/// Full-precision scale options for materialized lake data.
///
/// Lake decimals are stored at scale 18; carrying the same scale through the
/// Python-compatible conversion preserves every digit, unlike the 2-digit
/// synthetic benchmark scales.
fn lake_scale_options() -> SyntheticDataOptions {
    SyntheticDataOptions {
        base_price_scale: num::SCALE,
        tick_scale: num::SCALE,
        quantity_scale: num::SCALE,
        ..SyntheticDataOptions::default()
    }
}

/// Build a replay fixture from the market-lake read route.
///
/// Uses the declared configuration subscriptions. Backtests also acquire
/// separate quote/trade execution feeds. Equity symbols use the `equity` asset
/// class; symbols shaped like a crypto pair (`BTC-USD`) use the `crypto` asset
/// class with their canonical `BASE/QUOTE` symbol. Fetched crypto events are
/// re-keyed to the configured dashed symbol so the fixture, preflight, and
/// research context all agree.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the lake cannot serve the configured
/// window or the route payload cannot be decoded. There is no synthetic
/// fallback.
pub fn lake_fixture(
    configuration: &BacktestConfig,
    client: &BlockingLakeClient,
) -> Result<ReplayFixture, BacktestJobError> {
    lake_fixture_with_subscriptions(configuration, client, &configuration.data_subscriptions)
}

/// Acquire exact consumer inputs plus separate backtest execution feeds.
///
/// # Errors
/// Rejects incompatible intervals and empty requested symbol/type coverage.
pub fn lake_fixture_with_subscriptions(
    configuration: &BacktestConfig,
    client: &BlockingLakeClient,
    subscriptions: &[MarketSubscription],
) -> Result<ReplayFixture, BacktestJobError> {
    let requirements = normalize_subscriptions(subscriptions)?;
    if configuration.has_subscription_echo()
        && normalize_subscriptions(&configuration.data_subscriptions)? != requirements
    {
        return Err(BacktestJobError::Engine(
            "data.subscriptions does not match runtime subscription union".into(),
        ));
    }
    let mut acquisition = requirements.clone();
    if configuration.shell != "study" {
        acquisition.extend(super::engine_core::execution_subscriptions(&requirements));
    }
    let acquisition = normalize_subscriptions(&acquisition)?;
    let start_date = Date::from_days(configuration.start_ns.div_euclid(ONE_DAY_NS) as i32);
    let end_date = Date::from_days(
        configuration
            .end_ns
            .saturating_sub(1)
            .div_euclid(ONE_DAY_NS) as i32,
    );
    let start_text = start_date.isoformat();
    let end_text = end_date.isoformat();
    let mut events =
        lake_subscription_events(configuration, client, &start_text, &end_text, &acquisition)?;
    if events.is_empty() {
        let expected = if configuration.shell == "study" {
            "bar"
        } else {
            "quote or trade"
        };
        return Err(BacktestJobError::Io(format!(
            "the market lake served no {expected} events for the configured window \
             {start_text}..{end_text} (symbols: {}); ensure the lake covers the run window",
            configuration.symbols.join(", ")
        )));
    }
    events.sort_by(|left, right| {
        left.timestamp_ns()
            .cmp(&right.timestamp_ns())
            .then_with(|| left.sequence().cmp(&right.sequence()))
            .then_with(|| class_priority(left).cmp(&class_priority(right)))
    });
    let sessions = sessions_from_events(&events);
    let fingerprints = ReplayFingerprints::of_events(&events);
    let fixture = ReplayFixture {
        sessions,
        events,
        source_id: format!("lake:{}", client.base_url()),
        symbols: configuration.symbols.clone(),
        fingerprints,
        expected_data_fingerprint: None,
    };
    super::preflight::validate_subscription_coverage(&fixture, &requirements)?;
    if let Some(bars) = requirements.iter().find(|input| input.data_type == "bars") {
        let mut frame_config = configuration.clone();
        frame_config.symbols = bars.symbols.clone();
        frame_config.data_bar_interval = bars.interval.clone().unwrap();
        let frames = super::engine_core::assemble_study_frames(
            &frame_config,
            &fixture,
            &lake_scale_options(),
        )?;
        super::preflight::validate_completed_bar_coverage(&requirements, &frames)?;
    }
    Ok(fixture)
}

/// Ensure and read every normalized symbol/type requirement.
///
/// Every input window is ensured and read concurrently (bounded by the lake
/// client's fetch concurrency), then merged. Completion order cannot change the
/// replay: [`lake_fixture`] sorts the merged events by timestamp before any
/// preflight or engine work.
fn lake_subscription_events(
    configuration: &BacktestConfig,
    client: &BlockingLakeClient,
    start_text: &str,
    end_text: &str,
    subscriptions: &[MarketSubscription],
) -> Result<Vec<MarketEvent>, BacktestJobError> {
    let mut windows = Vec::new();
    for input in subscriptions {
        let data_type = match input.data_type.as_str() {
            "bars" => DataType::Bars,
            "quote" => DataType::Quote,
            "trade" => DataType::Trade,
            other => {
                return Err(BacktestJobError::from(BacktestError::Configuration(
                    format!("unknown data type {other:?}"),
                )))
            }
        };
        for symbol in &input.symbols {
            let (asset_class, lake_symbol) = match marketdata::crypto_pair(symbol) {
                Some(pair) => ("crypto", pair),
                None => ("equity", symbol.trim().to_uppercase()),
            };
            let mut ensure = EnsureSpec::new(
                asset_class,
                data_type.value(),
                &lake_symbol,
                start_text,
                end_text,
            );
            let mut read = ReadSpec::new(
                asset_class,
                data_type.value(),
                &lake_symbol,
                start_text,
                end_text,
            );
            if let Some(interval) = &input.interval {
                ensure = ensure.with_interval(interval);
                read = read.with_interval(interval);
            }
            windows.push((symbol.clone(), lake_symbol, data_type, ensure, read));
        }
    }
    let ensures: Vec<EnsureSpec> = windows
        .iter()
        .map(|(_, _, _, ensure, _)| ensure.clone())
        .collect();
    for (_, outcome) in windows.iter().zip(client.ensure_many(&ensures)) {
        outcome.map_err(|error| BacktestJobError::Io(error.to_string()))?;
    }
    let reads: Vec<ReadSpec> = windows
        .iter()
        .map(|(_, _, _, _, read)| read.clone())
        .collect();
    let mut events: Vec<MarketEvent> = Vec::new();
    for ((symbol, lake_symbol, data_type, _, _), batches) in
        windows.iter().zip(client.read_many(&reads))
    {
        let batches = batches.map_err(|error| BacktestJobError::Io(error.to_string()))?;
        let decoded = marketdata::events_from_arrow(*data_type, lake_symbol, &batches)
            .map_err(|error| BacktestJobError::Io(error.to_string()))?;
        // Marketdata DateTime is microsecond-based. Restore Arrow row precision
        // before enforcing the nanosecond run bounds; decoding preserves row order.
        let timestamps = batches.iter().flat_map(|batch| {
            let nanos = batch.column_by_name("timestamp").and_then(|column| {
                column
                    .as_any()
                    .downcast_ref::<arrow_array::TimestampNanosecondArray>()
            });
            (0..batch.num_rows()).map(move |row| nanos.map(|array| array.value(row)))
        });
        for (event, timestamp) in decoded.iter().zip(timestamps) {
            let mut event = lake_event(event, symbol);
            if let Some(timestamp) = timestamp {
                match &mut event {
                    MarketEvent::Quote { timestamp_ns, .. }
                    | MarketEvent::Trade { timestamp_ns, .. }
                    | MarketEvent::Bar { timestamp_ns, .. } => *timestamp_ns = timestamp,
                }
            }
            if event.timestamp_ns() >= configuration.start_ns
                && event.timestamp_ns() < configuration.end_ns
            {
                events.push(event);
            }
        }
    }
    Ok(events)
}

/// Convert one normalized lake event into the engine's fixture event.
fn lake_event(event: &marketdata::MarketEvent, symbol: &str) -> MarketEvent {
    match event {
        marketdata::MarketEvent::Quote(quote) => MarketEvent::Quote {
            event_id: quote.event_id.clone(),
            timestamp_ns: quote.timestamp.to_nanos(),
            sequence: quote.sequence,
            symbol: symbol.to_string(),
            bid: quote.bid_price,
            ask: quote.ask_price,
            bid_quantity: quote.bid_size,
            ask_quantity: quote.ask_size,
            venue: quote.venue.clone().unwrap_or_default(),
        },
        marketdata::MarketEvent::Trade(trade) => MarketEvent::Trade {
            event_id: trade.event_id.clone(),
            timestamp_ns: trade.timestamp.to_nanos(),
            sequence: trade.sequence,
            symbol: symbol.to_string(),
            price: trade.price,
            quantity: trade.quantity,
            venue: trade.venue.clone().unwrap_or_default(),
            trade_id: trade.trade_id.clone().unwrap_or_default(),
        },
        marketdata::MarketEvent::Bar(bar) => MarketEvent::Bar {
            event_id: bar.event_id.clone(),
            timestamp_ns: bar.timestamp.to_nanos(),
            sequence: bar.sequence,
            symbol: symbol.to_string(),
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
            volume: bar.volume,
        },
    }
}

const fn class_priority(event: &MarketEvent) -> u8 {
    match event {
        MarketEvent::Quote { .. } => 0,
        MarketEvent::Trade { .. } => 1,
        MarketEvent::Bar { .. } => marketdata::EventType::Bar.priority(),
    }
}

/// One deterministic UTC session window per day carrying events.
fn sessions_from_events(events: &[MarketEvent]) -> Vec<ExchangeSession> {
    let mut days: Vec<i64> = events
        .iter()
        .map(|event| event.timestamp_ns().div_euclid(ONE_DAY_NS))
        .collect();
    days.sort_unstable();
    days.dedup();
    days.into_iter()
        .map(|day| ExchangeSession {
            session_date_days: day,
            open_ns: day * ONE_DAY_NS,
            close_ns: (day + 1) * ONE_DAY_NS,
        })
        .collect()
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

/// Counts rendered in the `metrics.json` `counts` object.
struct JobCounts {
    market_events: u64,
    dispatched: u64,
    bar_frames: Option<u64>,
    orders: u64,
    fills: u64,
}

/// The deterministic metrics the job derives from the engine's fills.
struct JobLedger {
    equity: Vec<EquityPoint>,
    rows: Vec<LedgerRow>,
    fills: Vec<FillRow>,
    orders: Vec<OrderRow>,
}

/// One normalized fill shared by the injected and native engine paths.
struct LedgerFill {
    fill_id: String,
    order_id: String,
    symbol: String,
    side: String,
    quantity: String,
    price: String,
    fee: String,
    timestamp_ns: i64,
    sequence: u64,
}

/// One normalized order shared by the injected and native engine paths.
struct LedgerOrder {
    order_id: String,
    symbol: String,
    side: String,
    quantity: String,
}

fn parse_dec(value: &str) -> Decimal18 {
    value.parse::<Decimal18>().unwrap_or(Decimal18::ZERO)
}

fn ledger_fills_from_outcome(outcome: &BacktestOutcome) -> Vec<LedgerFill> {
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

fn ledger_orders_from_outcome(outcome: &BacktestOutcome) -> Vec<LedgerOrder> {
    let mut seen = BTreeSet::new();
    outcome
        .fills
        .iter()
        .filter(|fill| seen.insert(fill.order_id.clone()))
        .map(|fill| LedgerOrder {
            order_id: fill.order_id.clone(),
            symbol: fill.symbol.clone(),
            side: fill.side.clone(),
            quantity: fill.quantity.to_string(),
        })
        .collect()
}

fn build_ledger(
    configuration: &BacktestConfig,
    fills: &[LedgerFill],
    orders: &[LedgerOrder],
) -> JobLedger {
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
    let mut ledger_fills = Vec::new();
    for (index, fill) in fills.iter().enumerate() {
        let fill_count = index as u64 + 1;
        let quantity = parse_dec(&fill.quantity);
        let price = parse_dec(&fill.price);
        let fee = parse_dec(&fill.fee);
        let notional = quantity.checked_mul(price).unwrap_or(Decimal18::ZERO);
        let position = positions
            .entry(fill.symbol.clone())
            .or_insert(Decimal18::ZERO);
        if fill.side == "buy" {
            cash = cash
                .checked_sub(notional)
                .and_then(|value| value.checked_sub(fee))
                .unwrap_or(cash);
            *position = position.checked_add(quantity).unwrap_or(*position);
        } else {
            cash = cash
                .checked_add(notional)
                .and_then(|value| value.checked_sub(fee))
                .unwrap_or(cash);
            *position = position.checked_sub(quantity).unwrap_or(*position);
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
        ledger_fills.push(FillRow {
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
    let ledger_orders = orders
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
        fills: ledger_fills,
        orders: ledger_orders,
    }
}

fn decimal_opt(value: Option<Decimal18>) -> JsonValue {
    match value {
        Some(value) => JsonValue::String(value.to_string()),
        None => JsonValue::Null,
    }
}

fn metrics_json(
    run_id: &str,
    status: &str,
    counts: &JobCounts,
    ledger: &JobLedger,
    custom_metrics: &BTreeMap<String, String>,
) -> JsonValue {
    let metrics = equity_curve_metrics(&ledger.equity);
    let execution = execution_stats(&ledger.fills, &ledger.orders);
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
        ("run_id", JsonValue::String(run_id.to_string())),
        ("status", JsonValue::string(status)),
        ("counts", JsonValue::Object(count_values)),
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

fn write_metrics(
    directory: &Path,
    run_id: &str,
    status: &str,
    counts: &JobCounts,
    ledger: &JobLedger,
    custom_metrics: &BTreeMap<String, String>,
) -> Result<PathBuf, BacktestJobError> {
    let path = directory.join("metrics.json");
    let mut payload = metrics_json(run_id, status, counts, ledger, custom_metrics)
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
    orders: &[LedgerOrder],
    fills: &[LedgerFill],
    ledger: &JobLedger,
    custom_metrics: &BTreeMap<String, String>,
) -> Result<RunManifest, BacktestJobError> {
    let config = RecorderConfig::new(&configuration.run_id, directory)
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
        .map_err(|error| BacktestJobError::Engine(error.to_string()))
}
