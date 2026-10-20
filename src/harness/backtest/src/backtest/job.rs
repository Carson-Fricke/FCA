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

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::path::{Path, PathBuf};

use harness_contracts::progress::{ExecutionProgress, ExecutionStage, ProgressReporter};
use harness_lakeclient::{BlockingLakeClient, EnsureSpec, ReadSpec};
use num::{Decimal18, DecimalError};

use marketdata::{DataType, Date};

use crate::backtest::config::BacktestConfig;
use crate::backtest::engine_core::{
    build_default_manifest, normalize_subscriptions, run_native_engine, runtime_subscriptions,
    BacktestOutcome, EngineOptions, MarketSubscription, StrategyInstance,
};
use crate::backtest::errors::{BacktestError, ONE_DAY_NS};
use crate::backtest::fixture::{MarketEvent, ReplayFixture, SyntheticDataOptions};
use crate::backtest::preflight::preflight_data;
use crate::backtest::screen::ScreenConfig;
use crate::canonical_json::JsonValue;
use crate::results::metrics::{
    execution_stats, trade_stats, EquityAccumulator, EquityMetrics, EquityPoint, ExecutionStats,
    FillRow, MetricsError, OrderRow, Outcome, TradeRow, TradeStats,
};
use crate::results::recorder::{
    ActionKind, ActionRecord, FillRecord, PortfolioEquityRecord, RecorderConfig, RunRecorder,
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
    /// A fill's quantity, price, fee, or side is malformed, or its notional
    /// leaves the decimal128(38,18) bound. The job fails; no partial result is
    /// written.
    MalformedFill {
        /// The fill that failed.
        fill_id: String,
        /// What is malformed about it.
        detail: String,
    },
    /// Exact ledger or metric arithmetic left the decimal128(38,18) bound.
    Overflow(String),
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
            Self::MalformedFill { fill_id, detail } => {
                write!(formatter, "fill {fill_id} is malformed: {detail}")
            }
            Self::Overflow(message) => write!(formatter, "{message}"),
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

/// The fixed identity used only for authored-program validation documents.
pub const VALIDATION_JOB_ID: &str = "authored-validation";

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
    load_backtest_config_with_job_id(config_path, None)
}

/// Load one backtest configuration with an explicit submission `job_id`.
///
/// The submitted `job_id` is applied to the raw document before validation;
/// it replaces any value already present because one authored document can
/// serve many executions.
///
/// # Errors
///
/// Returns [`BacktestJobError`] under the same conditions as
/// [`load_backtest_config`], and for an invalid submitted `job_id`.
pub fn load_backtest_config_with_job_id(
    config_path: &Path,
    job_id: Option<&str>,
) -> Result<BacktestConfig, BacktestJobError> {
    load_replay_submission(config_path, job_id, "backtest").map(|(configuration, _)| configuration)
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
    load_study_config_with_job_id(config_path, None)
}

/// Load one study configuration with an explicit submission `job_id`.
///
/// # Errors
///
/// Returns [`BacktestJobError`] under the same conditions as
/// [`load_study_config`], and for an invalid submitted `job_id`.
pub fn load_study_config_with_job_id(
    config_path: &Path,
    job_id: Option<&str>,
) -> Result<BacktestConfig, BacktestJobError> {
    load_replay_submission(config_path, job_id, "study").map(|(configuration, _)| configuration)
}

/// Capture the admitted document and resolved replay configuration from one read.
///
/// # Errors
/// Returns a configuration error before admission for invalid shell, id or source.
pub fn load_replay_submission(
    config_path: &Path,
    job_id: Option<&str>,
    expected_shell: &str,
) -> Result<(BacktestConfig, serde_json::Value), BacktestJobError> {
    let text = std::fs::read_to_string(config_path)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let shell = parse_shell(&text).ok_or(BacktestJobError::MissingShell)?;
    if shell != expected_shell {
        return Err(BacktestJobError::WrongShell(shell));
    }
    let mut document = config::replay::submitted_document(&text)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    overlay_job_id_json(&mut document, job_id)?;
    let configuration = resolve_authored_sources(
        BacktestConfig::from_yaml(&document.to_string()).map_err(BacktestJobError::from)?,
        config_path,
    )?;
    document["job_id"] = serde_json::Value::String(configuration.job_id.clone());
    if let Some(instances) = document
        .get_mut("strategy_instances")
        .and_then(serde_json::Value::as_array_mut)
    {
        for (document, instance) in instances.iter_mut().zip(&configuration.strategy_instances) {
            if let Some(source) = instance.parameters.get("source") {
                let source =
                    serde_json::from_str::<serde_json::Value>(&source.to_canonical_string())
                        .map_err(|e| BacktestJobError::Io(e.to_string()))?;
                document["parameters"]["source"] = source;
                if let Some(parameters) = document
                    .get_mut("parameters")
                    .and_then(serde_json::Value::as_object_mut)
                {
                    parameters.remove("source_path");
                }
            }
        }
    }
    Ok((configuration, document))
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
    load_screen_config_with_job_id(config_path, None)
}

/// Load one screening configuration with an explicit submission `job_id`.
///
/// # Errors
///
/// Returns [`BacktestJobError`] under the same conditions as
/// [`load_screen_config`], and for an invalid submitted `job_id`.
pub fn load_screen_config_with_job_id(
    config_path: &Path,
    job_id: Option<&str>,
) -> Result<ScreenConfig, BacktestJobError> {
    let text = std::fs::read_to_string(config_path)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let shell = parse_shell(&text).ok_or(BacktestJobError::MissingShell)?;
    if shell != "screen" {
        return Err(BacktestJobError::WrongScreenShell(shell));
    }
    let base_dir = config_path.parent();
    if text.trim_start().starts_with('{') {
        let mut document: serde_json::Value =
            serde_json::from_str(&text).map_err(|error| BacktestJobError::Io(error.to_string()))?;
        overlay_job_id_json(&mut document, job_id)?;
        return ScreenConfig::from_json_value_with_base(&document, base_dir)
            .map_err(BacktestJobError::from);
    }
    let mut value = crate::backtest::yaml::load_yaml(&text)
        .map_err(|error| BacktestJobError::from(BacktestError::Configuration(error.0)))?;
    overlay_job_id_yaml(&mut value, job_id)?;
    ScreenConfig::from_yaml_value_with_base(&value, base_dir).map_err(BacktestJobError::from)
}

/// Apply an explicit submission `job_id` to a parsed JSON configuration.
///
/// The submission argument wins and replaces any value already present.
///
/// # Errors
///
/// Returns [`BacktestJobError::Engine`] for an invalid submitted `job_id` or a
/// non-mapping document.
pub fn overlay_job_id_json(
    document: &mut serde_json::Value,
    job_id: Option<&str>,
) -> Result<bool, BacktestJobError> {
    let Some(job_id) = job_id else {
        return Ok(false);
    };
    let job_id = validated_submitted_job_id(job_id)?;
    let object = document.as_object_mut().ok_or_else(|| {
        BacktestJobError::Engine("configuration root must be a mapping".to_string())
    })?;
    object.insert("job_id".to_string(), serde_json::Value::String(job_id));
    Ok(true)
}

/// Apply an explicit submission `job_id` to a parsed YAML configuration.
///
/// The submission argument wins and replaces any value already present.
///
/// # Errors
///
/// Returns [`BacktestJobError::Engine`] under the same conditions as
/// [`overlay_job_id_json`].
pub fn overlay_job_id_yaml(
    document: &mut crate::backtest::yaml::YamlValue,
    job_id: Option<&str>,
) -> Result<bool, BacktestJobError> {
    let Some(job_id) = job_id else {
        return Ok(false);
    };
    let job_id = validated_submitted_job_id(job_id)?;
    let crate::backtest::yaml::YamlValue::Mapping(mapping) = document else {
        return Err(BacktestJobError::Engine(
            "configuration root must be a mapping".to_string(),
        ));
    };
    mapping.insert(
        "job_id".to_string(),
        crate::backtest::yaml::YamlValue::String(job_id),
    );
    Ok(true)
}

fn validated_submitted_job_id(value: &str) -> Result<String, BacktestJobError> {
    let trimmed = value.trim();
    if trimmed.is_empty()
        || trimmed.len() > 128
        || trimmed.contains('/')
        || trimmed.contains('\\')
        || trimmed == "."
        || trimmed == ".."
    {
        return Err(BacktestJobError::Engine(
            "job_id must be a single safe path segment of at most 128 characters".to_string(),
        ));
    }
    Ok(trimmed.to_string())
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
        instance.parameters.remove("source_path");
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
/// The run window's declared data is ensured and streamed through the
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
    let mut source_configuration = configuration.clone();
    source_configuration.data_source_bar_interval = "1min".into();
    client
        .report_progress(ExecutionProgress::stage(ExecutionStage::RunningBacktest))
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let outcome = run_native_engine_outcome(
        &source_configuration,
        engine_options,
        strategies,
        fixture,
        &lake_scale_options(),
    )?;
    client
        .report_progress(ExecutionProgress::stage(ExecutionStage::ComputingResults))
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let artifact_path = write_run_artifacts(
        configuration,
        &outcome,
        output_dir,
        "backtests",
        "backtest.json",
        client.progress_reporter().as_ref(),
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
    let mut source_configuration = configuration.clone();
    source_configuration.data_source_bar_interval = "1min".into();
    client
        .report_progress(ExecutionProgress::stage(ExecutionStage::RunningStudy))
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let outcome = run_native_engine_outcome(
        &source_configuration,
        engine_options,
        programs,
        fixture,
        &lake_scale_options(),
    )?;
    client
        .report_progress(ExecutionProgress::stage(ExecutionStage::ComputingResults))
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let artifact_path = write_run_artifacts(
        configuration,
        &outcome,
        output_dir,
        "studies",
        "study.json",
        client.progress_reporter().as_ref(),
    )?;
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
    // The null transform is a data boundary: it runs before any preflight or
    // manifest identity is derived so every downstream record describes the
    // transformed events, and its recomputed fingerprints match the replay.
    let fixture = super::null::apply_null_transform(configuration, &fixture)?;
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
///
/// Ledger arithmetic is validated before encoding. The exact variance pass
/// shares the artifact reader; immutable upload and atomic visibility belong
/// to the result store, after this complete local result set has been written.
fn write_run_artifacts(
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
        ledger.rows.len() as u64,
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
fn write_native_results(
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
/// Rejects unsupported cadences and empty requested symbol/type coverage.
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
    let acquisition = super::engine_core::source_subscriptions(&acquisition, "1min")?;
    let start_date = Date::from_days(configuration.start_ns.div_euclid(ONE_DAY_NS) as i32);
    let end_date = Date::from_days(
        configuration
            .end_ns
            .saturating_sub(1)
            .div_euclid(ONE_DAY_NS) as i32,
    );
    let start_text = start_date.isoformat();
    let end_text = end_date.isoformat();
    let mut resolved = BTreeMap::new();
    for (alias, selector) in &configuration.option_selectors {
        let as_of = selector
            .validate()
            .map_err(|e| BacktestJobError::Io(e.to_string()))?;
        if as_of.to_nanos_i64() > configuration.start_ns {
            return Err(BacktestJobError::Io(
                "option selection must not follow replay start".into(),
            ));
        }
        if !configuration.symbols.contains(alias) {
            return Err(BacktestJobError::Io(
                "option input alias must be a configured symbol".into(),
            ));
        }
        resolved.insert(
            alias.clone(),
            client
                .resolve_option(selector)
                .map_err(|e| BacktestJobError::Io(e.to_string()))?,
        );
    }
    let (stream, events, sessions) = lake_subscription_events(
        configuration,
        client,
        &start_text,
        &end_text,
        &acquisition,
        &resolved,
    )?;
    if stream.evidence.event_count == 0 {
        return Err(BacktestJobError::Io(
            "the market lake served no requested events for the configured window".into(),
        ));
    }
    let fingerprints = stream.evidence.fingerprints.clone();
    let fixture = ReplayFixture {
        sessions,
        events,
        stream: Some(stream.clone()),
        source_id: stream.evidence.source_id.clone(),
        symbols: configuration.symbols.clone(),
        fingerprints,
        expected_data_fingerprint: None,
    };
    super::preflight::validate_subscription_coverage(&fixture, &requirements)?;
    let mut frame_config = configuration.clone();
    frame_config.data_source_bar_interval = "1min".into();
    let frames = super::engine_core::assemble_subscription_frames(
        &frame_config,
        &fixture,
        &lake_scale_options(),
        &requirements,
    )?;
    super::preflight::validate_completed_bar_coverage(&requirements, &frames)?;
    Ok(fixture)
}

/// Ensure and read every normalized symbol/type requirement.
///
/// Input windows are ensured concurrently. Arrow batches are then decoded and
/// consumed incrementally, releasing each batch before the next arrives. Completion order cannot change the
/// replay: [`lake_fixture`] sorts the merged events by timestamp before any
/// preflight or engine work.
fn lake_subscription_events(
    configuration: &BacktestConfig,
    client: &BlockingLakeClient,
    start_text: &str,
    end_text: &str,
    subscriptions: &[MarketSubscription],
    resolved: &BTreeMap<String, harness_contracts::ResolvedOption>,
) -> Result<super::stream::PlannedReplay, BacktestJobError> {
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
            let (asset_class, lake_symbol) = if let Some(contract) = resolved.get(symbol) {
                ("option", contract.symbol.clone())
            } else if marketdata::lake::canonical_osi_symbol(symbol).is_ok() {
                ("option", symbol.trim().to_uppercase())
            } else {
                match marketdata::crypto_pair(symbol) {
                    Some(pair) => ("crypto", pair),
                    None => ("equity", symbol.trim().to_uppercase()),
                }
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
    client
        .report_progress(ExecutionProgress::stage(ExecutionStage::ReadingData))
        .map_err(|e| BacktestJobError::Io(e.to_string()))?;
    let cursor = std::sync::Arc::new(
        client
            .replay_cursor()
            .map_err(|e| BacktestJobError::Io(e.to_string()))?,
    );
    let inputs = windows
        .into_iter()
        .map(
            |(symbol, lake_symbol, data_type, _, read)| super::stream::LakeInput {
                symbol,
                lake_symbol,
                data_type,
                read,
            },
        )
        .collect();
    let source_id = if resolved.is_empty() {
        format!("lake:{}", client.base_url())
    } else {
        format!(
            "lake:{};options={}",
            client.base_url(),
            serde_json::to_string(resolved).map_err(|e| BacktestJobError::Io(e.to_string()))?
        )
    };
    super::stream::StreamedReplay::catalog(
        cursor,
        inputs,
        source_id,
        configuration.start_ns,
        configuration.end_ns,
        client.progress_reporter(),
    )
    .map_err(BacktestJobError::from)
}

/// Convert one normalized lake event into the engine's fixture event.
pub(super) fn lake_event(event: &marketdata::MarketEvent, symbol: &str) -> MarketEvent {
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

/// One point of the deterministic job ledger derived from the run's fills.
#[derive(Clone, Debug)]
struct LedgerRow {
    timestamp_ns: i64,
    cash: Decimal18,
    equity: Decimal18,
    market_value: Decimal18,
    position_count: u32,
    fill_count: u64,
}

impl super::history::Record for LedgerRow {
    fn encode(&self, output: &mut Vec<u8>) {
        output.extend(self.timestamp_ns.to_le_bytes());
        for value in [self.cash, self.equity, self.market_value] {
            output.extend(value.unscaled().to_le_bytes());
        }
        output.extend(self.position_count.to_le_bytes());
        output.extend(self.fill_count.to_le_bytes());
    }
    fn decode(input: &mut &[u8]) -> std::io::Result<Self> {
        use super::history::take;
        Ok(Self {
            timestamp_ns: i64::from_le_bytes(take(input)?),
            cash: Decimal18::from_unscaled(i128::from_le_bytes(take(input)?))
                .map_err(std::io::Error::other)?,
            equity: Decimal18::from_unscaled(i128::from_le_bytes(take(input)?))
                .map_err(std::io::Error::other)?,
            market_value: Decimal18::from_unscaled(i128::from_le_bytes(take(input)?))
                .map_err(std::io::Error::other)?,
            position_count: u32::from_le_bytes(take(input)?),
            fill_count: u64::from_le_bytes(take(input)?),
        })
    }
}

/// Counts rendered in the `metrics.json` `counts` object.
struct JobCounts {
    market_events: u64,
    dispatched: u64,
    bar_frames: Option<u64>,
    orders: u64,
    fills: u64,
}

/// The deterministic metrics the job derives from the engine's fills. Every
/// statistic is computed once here, before any artifact is written.
struct JobLedger {
    rows: super::history::History<LedgerRow>,
    trades: ClosedTrades,
    trade_stats: TradeStats,
    equity_summary: EquityAccumulator,
    chart: Vec<EquityPoint>,
    net_pnl: Decimal18,
    gross_pnl: Decimal18,
    execution: ExecutionStats,
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

/// Parse one configuration decimal. A malformed value fails the job; it never
/// defaults to zero, which would silently change PnL.
fn ledger_decimal(value: &str, field: &str) -> Result<Decimal18, BacktestJobError> {
    value.parse::<Decimal18>().map_err(|error| {
        BacktestJobError::Engine(format!("{field} {value:?} is not a decimal: {error}"))
    })
}

/// Parse one field of a fill. A malformed value fails the job as a
/// [`BacktestJobError::MalformedFill`]; it never defaults to zero.
fn fill_decimal(
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

fn malformed_fill(fill: &LedgerFill, detail: impl Into<String>) -> BacktestJobError {
    BacktestJobError::MalformedFill {
        fill_id: fill.fill_id.clone(),
        detail: detail.into(),
    }
}

/// Map one exact operation that left the decimal bound onto the job error.
fn overflow_ledger(
    result: Result<Decimal18, DecimalError>,
    operation: &str,
) -> Result<Decimal18, BacktestJobError> {
    result.map_err(|_| {
        BacktestJobError::Overflow(format!("{operation} overflows the decimal128(38,18) bound"))
    })
}

/// Map one exact operation on a fill's own fields onto a malformed-fill error.
fn overflow_fill(
    fill: &LedgerFill,
    result: Result<Decimal18, DecimalError>,
    operation: &str,
) -> Result<Decimal18, BacktestJobError> {
    result.map_err(|_| {
        malformed_fill(
            fill,
            format!("{operation} overflows the decimal128(38,18) bound"),
        )
    })
}

fn metrics_overflow(error: MetricsError) -> BacktestJobError {
    BacktestJobError::Overflow(error.to_string())
}

/// Direction of one fill: `true` for a buy, `false` for a sell. The side is an
/// exact lowercase comparison; any other value fails the job.
fn fill_is_buy(fill: &LedgerFill) -> Result<bool, BacktestJobError> {
    match fill.side.as_str() {
        "buy" => Ok(true),
        "sell" => Ok(false),
        other => Err(malformed_fill(
            fill,
            format!("side {other:?} is neither \"buy\" nor \"sell\""),
        )),
    }
}

/// `amount * part / whole`, taking the whole amount when `part` is all of
/// `whole` so that allocated shares sum to the amount exactly.
fn fee_share(
    amount: Decimal18,
    part: Decimal18,
    whole: Decimal18,
) -> Result<Decimal18, BacktestJobError> {
    if part == whole {
        return Ok(amount);
    }
    overflow_ledger(amount.checked_mul_div(part, whole), "fee allocation")
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

/// One open FIFO lot: the unmatched quantity of one opening fill and the fee
/// still attached to it.
struct OpenLot {
    long: bool,
    sequence: u64,
    timestamp_ns: i64,
    price: Decimal18,
    quantity: Decimal18,
    fee: Decimal18,
}

/// Closed trade slices plus the fees still attached to open lots at run end.
struct ClosedTrades {
    rows: Vec<TradeRow>,
    open_fees: Decimal18,
}

/// Derive closed trades from one fill ledger with FIFO matching.
///
/// Fills are ordered by `(sequence, timestamp)`. A fill closes opposite lots
/// from the front of its symbol's queue and opens a lot with any unmatched
/// remainder, so position flips keep the remainder. Each slice carries its
/// share of both fees by fill quantity (`fill_quantity_share.v1`), so the
/// closed-slice fees plus `open_fees` equal the total fees exactly.
fn closed_trades(fills: &[LedgerFill]) -> Result<ClosedTrades, BacktestJobError> {
    let mut ordered: Vec<&LedgerFill> = fills.iter().collect();
    ordered.sort_by_key(|fill| (fill.sequence, fill.timestamp_ns / 1_000_000));
    let mut lots: BTreeMap<String, VecDeque<OpenLot>> = BTreeMap::new();
    let mut rows: Vec<TradeRow> = Vec::new();
    for fill in ordered {
        let is_buy = fill_is_buy(fill)?;
        let quantity = fill_decimal(fill, "quantity", &fill.quantity)?;
        let fee = fill_decimal(fill, "fee", &fill.fee)?;
        let price = fill_decimal(fill, "price", &fill.price)?;
        if quantity.is_zero() {
            if fee.is_zero() {
                continue;
            }
            return Err(malformed_fill(fill, "fee is non-zero but quantity is zero"));
        }
        if quantity.is_negative() {
            return Err(malformed_fill(fill, "quantity is negative"));
        }
        let queue = lots.entry(fill.symbol.clone()).or_default();
        let mut remaining = quantity;
        let mut remaining_fee = fee;
        while remaining.is_positive() {
            let Some(head) = queue.front() else {
                break;
            };
            if head.long == is_buy {
                break;
            }
            let (long, lot_sequence, lot_timestamp, lot_price, lot_quantity, lot_fee) = (
                head.long,
                head.sequence,
                head.timestamp_ns,
                head.price,
                head.quantity,
                head.fee,
            );
            let matched = lot_quantity.min(remaining);
            let entry_fee = fee_share(lot_fee, matched, lot_quantity)?;
            let exit_fee = fee_share(remaining_fee, matched, remaining)?;
            let difference = if long {
                overflow_ledger(price.checked_sub(lot_price), "trade price difference")?
            } else {
                overflow_ledger(lot_price.checked_sub(price), "trade price difference")?
            };
            let gross_pnl = overflow_ledger(difference.checked_mul(matched), "trade gross pnl")?;
            let fees = overflow_ledger(entry_fee.checked_add(exit_fee), "trade fees")?;
            let net_pnl = overflow_ledger(gross_pnl.checked_sub(fees), "trade net pnl")?;
            rows.push(TradeRow {
                trade_id: rows.len() as u64 + 1,
                symbol: fill.symbol.clone(),
                side: if long { "long" } else { "short" }.to_string(),
                quantity: matched,
                entry_sequence: lot_sequence,
                exit_sequence: fill.sequence,
                entry_timestamp_ns: lot_timestamp,
                exit_timestamp_ns: fill.timestamp_ns,
                entry_price: lot_price,
                exit_price: price,
                gross_pnl,
                entry_fee,
                exit_fee,
                fees,
                net_pnl,
            });
            let head_done = {
                let Some(head) = queue.front_mut() else {
                    return Err(BacktestJobError::Engine(
                        "open lot queue lost its head while matching".to_string(),
                    ));
                };
                head.quantity =
                    overflow_ledger(head.quantity.checked_sub(matched), "open lot quantity")?;
                head.fee = overflow_ledger(head.fee.checked_sub(entry_fee), "open lot fee")?;
                head.quantity.is_zero()
            };
            if head_done {
                queue.pop_front();
            }
            remaining = overflow_ledger(remaining.checked_sub(matched), "fill quantity")?;
            remaining_fee = overflow_ledger(remaining_fee.checked_sub(exit_fee), "fill fee")?;
        }
        if remaining.is_positive() {
            queue.push_back(OpenLot {
                long: is_buy,
                sequence: fill.sequence,
                timestamp_ns: fill.timestamp_ns,
                price,
                quantity: remaining,
                fee: remaining_fee,
            });
        }
    }
    let mut open_fees = Decimal18::ZERO;
    for queue in lots.values() {
        for lot in queue {
            open_fees = overflow_ledger(open_fees.checked_add(lot.fee), "open fees")?;
        }
    }
    Ok(ClosedTrades { rows, open_fees })
}

fn report_results(
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

fn build_ledger(
    configuration: &BacktestConfig,
    fills: &[LedgerFill],
    orders: &[LedgerOrder],
    valuations: &super::history::History<super::engine_core::ValuationSample>,
    reporter: Option<&ProgressReporter>,
) -> Result<JobLedger, BacktestJobError> {
    let mut cash = ledger_decimal(&configuration.initial_cash, "initial_cash")?;
    let mut positions: BTreeMap<String, Decimal18> = BTreeMap::new();
    let mut marks: BTreeMap<String, Decimal18> = BTreeMap::new();
    let mut rows = super::history::History::default();
    let mut summary = EquityAccumulator::default();
    let mut chart = Vec::new();
    let total = valuations.len() + 1;
    let stride = total.div_ceil(2000).max(1);
    let mut last_report = std::time::Instant::now();
    let mut push_row = |row: LedgerRow| -> Result<(), BacktestJobError> {
        let point = EquityPoint {
            timestamp_ns: row.timestamp_ns,
            equity: row.equity,
        };
        summary.push(point);
        if rows.len().is_multiple_of(stride) || rows.len() + 1 == total {
            chart.push(point);
        }
        rows.push(row)
            .map_err(|e| BacktestJobError::Io(e.to_string()))?;
        if rows.len().is_multiple_of(8192) && last_report.elapsed().as_secs() >= 1 {
            report_results(
                reporter,
                ExecutionStage::ComputingResults,
                rows.len() as u64,
                false,
            )?;
            last_report = std::time::Instant::now();
        }
        Ok(())
    };
    push_row(LedgerRow {
        timestamp_ns: configuration.start_ns,
        cash,
        equity: cash,
        market_value: Decimal18::ZERO,
        position_count: 0,
        fill_count: 0,
    })
    .map_err(|e| BacktestJobError::Io(e.to_string()))?;
    let mut ledger_fills = Vec::new();
    let mut last_timestamp_ns = configuration.start_ns;
    let mut consumed_fills = 0;
    for sample in valuations
        .iter()
        .map_err(|e| BacktestJobError::Io(e.to_string()))?
    {
        let sample = sample.map_err(|e| BacktestJobError::Io(e.to_string()))?;
        if sample.timestamp_ns < last_timestamp_ns
            || sample.fill_count < consumed_fills
            || sample.fill_count > fills.len()
        {
            return Err(BacktestJobError::Engine(
                "valuation history regresses time or names an invalid fill prefix".into(),
            ));
        }
        last_timestamp_ns = sample.timestamp_ns;
        for fill in &fills[consumed_fills..sample.fill_count] {
            if fill.timestamp_ns > sample.timestamp_ns {
                return Err(BacktestJobError::Engine(format!(
                    "valuation uses fill {} before it became available",
                    fill.fill_id
                )));
            }
            let is_buy = fill_is_buy(fill)?;
            let quantity = fill_decimal(fill, "quantity", &fill.quantity)?;
            let price = fill_decimal(fill, "price", &fill.price)?;
            let fee = fill_decimal(fill, "fee", &fill.fee)?;
            let notional = overflow_fill(
                fill,
                quantity.checked_mul(price),
                "notional (quantity times price)",
            )?;
            let position = positions
                .entry(fill.symbol.clone())
                .or_insert(Decimal18::ZERO);
            if is_buy {
                cash = overflow_ledger(
                    cash.checked_sub(notional)
                        .and_then(|value| value.checked_sub(fee)),
                    "cash after a buy",
                )?;
                *position = overflow_ledger(position.checked_add(quantity), "position")?;
            } else {
                cash = overflow_ledger(
                    cash.checked_add(notional)
                        .and_then(|value| value.checked_sub(fee)),
                    "cash after a sell",
                )?;
                *position = overflow_ledger(position.checked_sub(quantity), "position")?;
            }
            ledger_fills.push(FillRow {
                fill_id: fill.fill_id.clone(),
                order_id: fill.order_id.clone(),
                strategy_id: String::new(),
                quantity,
                price,
                fee,
                latency_ns: None,
            });
        }
        consumed_fills = sample.fill_count;
        for (symbol, mark) in &sample.marks {
            marks.insert(
                symbol.clone(),
                Decimal18::from_decimal(*mark).map_err(|e| {
                    BacktestJobError::Engine(format!("valuation mark cannot be represented: {e}"))
                })?,
            );
        }
        let mut market_value = Decimal18::ZERO;
        for (symbol, held) in &positions {
            if held.is_zero() {
                continue;
            }
            let mark = marks.get(symbol).ok_or_else(|| {
                BacktestJobError::Engine(format!(
                    "open position {symbol:?} has no observed valuation mark"
                ))
            })?;
            let value = overflow_ledger(held.checked_mul(*mark), "position market value")?;
            market_value = overflow_ledger(market_value.checked_add(value), "market value")?;
        }
        let equity_value = overflow_ledger(cash.checked_add(market_value), "equity")?;
        push_row(LedgerRow {
            timestamp_ns: sample.timestamp_ns,
            cash,
            equity: equity_value,
            market_value,
            position_count: positions.values().filter(|held| !held.is_zero()).count() as u32,
            fill_count: sample.fill_count as u64,
        })
        .map_err(|e| BacktestJobError::Io(e.to_string()))?;
    }
    if consumed_fills != fills.len() {
        return Err(BacktestJobError::Engine(
            "valuation history does not cover every booked fill".into(),
        ));
    }
    let ledger_orders: Vec<OrderRow> = orders
        .iter()
        .map(|order| OrderRow {
            order_id: order.order_id.clone(),
            strategy_id: String::new(),
            outcome: "filled".to_string(),
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
    Ok(JobLedger {
        rows,
        trades,
        trade_stats,
        equity_summary: summary,
        chart,
        net_pnl,
        gross_pnl,
        execution,
    })
}

fn decimal_opt(value: Option<Decimal18>) -> JsonValue {
    match value {
        Some(value) => JsonValue::String(value.to_string()),
        None => JsonValue::Null,
    }
}

/// A decimal in the canonical Decimal18 text (Python `str(Decimal)` form).
fn decimal_text(value: Decimal18) -> JsonValue {
    JsonValue::String(value.to_string())
}

/// The reason an optional statistic is null.
fn reason_opt(reason: Option<&'static str>) -> JsonValue {
    match reason {
        Some(reason) => JsonValue::string(reason),
        None => JsonValue::Null,
    }
}

/// An equity decimal: its canonical text, or null when it is undefined.
fn decimal_outcome(outcome: Outcome<Decimal18>) -> JsonValue {
    match outcome {
        Ok(value) => JsonValue::String(value.to_string()),
        Err(_) => JsonValue::Null,
    }
}

/// An equity ratio as a decimal string with 18 fractional digits, or null when
/// it is undefined.
fn float_outcome(outcome: Outcome<f64>) -> JsonValue {
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
fn trade_row_json(row: &TradeRow) -> JsonValue {
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
    ])
}

fn metrics_json(
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
        ("counts", JsonValue::Object(count_values)),
        (
            "equity",
            JsonValue::object([
                ("samples", JsonValue::UInt(ledger.rows.len() as u64)),
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

fn write_metrics(
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

fn write_report(
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

fn record_artifacts(
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

    let mut variance = ledger.equity_summary.variance();
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
        variance.push(EquityPoint {
            timestamp_ns: row.timestamp_ns,
            equity: row.equity,
        });
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
    Ok(ledger.equity_summary.clone().finish(variance))
}
