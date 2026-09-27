//! Local-mode submitters for the harness MCP server.
//!
//! These adapters are composed by [`crate::mcp_cli`] so that the MCP
//! `run_backtest` tool works in local mode. They drive the
//! native Rust engine with JIT-authored Rust programs, persist every run and
//! artifact into the shared durable results store, and return the same
//! payload shapes the remote API would return.

use std::collections::BTreeMap;
use std::fs;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::{DateTime, NaiveDate, Utc};
use serde_json::{json, Value};

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::EngineOptions;

use crate::lake_ensure::LakeEnsureClient;
use mcp::store::{JobRepository, JobState, RunCatalogRecord, RunStatus, RunStore};

/// The lake data types every configured symbol is ensured for.
const LAKE_DATA_TYPES: [&str; 2] = ["trade", "quote"];

/// Local backtest submitter that runs the native engine and records the run.
pub struct NativeBacktestSubmitter {
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
}

impl NativeBacktestSubmitter {
    /// Compose a submitter over the shared durable store.
    #[must_use]
    pub fn new(run_store: Arc<dyn RunStore>, jobs: Arc<dyn JobRepository>) -> Self {
        Self { run_store, jobs }
    }
}

impl mcp::tools::BacktestSubmitter for NativeBacktestSubmitter {
    fn submit(&self, config_path: &str, workspace: Option<&str>) -> Result<Value, String> {
        let configuration = backtest::backtest::job::load_backtest_config(Path::new(config_path))
            .map_err(|error| error.to_string())?;
        let run_id = configuration.run_id.clone();
        let started = Utc::now();
        self.jobs.upsert(mcp::store::JobRecord::new(
            &run_id,
            JobState::Running,
            started,
            started,
        ));
        let output_dir = workspace.map(PathBuf::from).unwrap_or_else(|| {
            std::env::temp_dir()
                .join("harness-mcp-backtests")
                .join(&run_id)
        });
        let run_store = self.run_store.clone();
        let jobs = self.jobs.clone();
        let config_path = config_path.to_string();
        let background_run_id = run_id.clone();
        let _handle = std::thread::spawn(move || {
            run_background_backtest(
                run_store,
                jobs,
                background_run_id,
                config_path,
                output_dir,
                started,
            );
        });
        Ok(json!({
            "boundary": "local",
            "run_id": run_id,
            "state": "running",
        }))
    }
}

/// Local study submitter that runs the native engine study path and records it.
pub struct NativeStudySubmitter {
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
}

impl NativeStudySubmitter {
    /// Compose a submitter over the shared durable store.
    #[must_use]
    pub fn new(run_store: Arc<dyn RunStore>, jobs: Arc<dyn JobRepository>) -> Self {
        Self { run_store, jobs }
    }
}

impl mcp::tools::StudySubmitter for NativeStudySubmitter {
    fn submit(&self, config_path: &str, workspace: Option<&str>) -> Result<Value, String> {
        let configuration = backtest::backtest::job::load_study_config(Path::new(config_path))
            .map_err(|error| error.to_string())?;
        let run_id = configuration.run_id.clone();
        let started = Utc::now();
        self.jobs.upsert(mcp::store::JobRecord::new(
            &run_id,
            JobState::Running,
            started,
            started,
        ));
        let output_dir = workspace.map(PathBuf::from).unwrap_or_else(|| {
            std::env::temp_dir()
                .join("harness-mcp-studies")
                .join(&run_id)
        });
        let run_store = self.run_store.clone();
        let jobs = self.jobs.clone();
        let config_path = config_path.to_string();
        let background_run_id = run_id.clone();
        let _handle = std::thread::spawn(move || {
            run_background_study(
                run_store,
                jobs,
                background_run_id,
                config_path,
                output_dir,
                started,
            );
        });
        Ok(json!({
            "boundary": "local",
            "study_id": run_id,
            "state": "running",
            "kind": "study",
        }))
    }
}

/// Run one study on a detached worker and persist its complete result set.
fn run_background_study(
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    run_id: String,
    config_path: String,
    output_dir: PathBuf,
    started: DateTime<Utc>,
) {
    let guard = BackgroundJobGuard::new(jobs.clone(), &run_id, started);
    let outcome = catch_unwind(AssertUnwindSafe({
        let run_store = run_store.clone();
        let jobs = jobs.clone();
        let run_id = run_id.clone();
        let config_path = config_path.clone();
        let output_dir = output_dir.clone();
        move || {
            run_background_study_inner(run_store, jobs, run_id, config_path, output_dir, started);
        }
    }));
    if let Err(payload) = outcome {
        fail_job(
            &jobs,
            &run_id,
            started,
            format!(
                "the background study panicked: {}",
                panic_message(payload.as_ref())
            ),
        );
    }
    drop(guard);
}

/// The study body. Every exit path writes a terminal job record.
fn run_background_study_inner(
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    run_id: String,
    config_path: String,
    output_dir: PathBuf,
    started: DateTime<Utc>,
) {
    let configuration = match backtest::backtest::job::load_study_config(Path::new(&config_path)) {
        Ok(configuration) => configuration,
        Err(error) => {
            let message = error.to_string();
            let mut artifacts = BTreeMap::new();
            artifacts.insert(
                "error.json".to_string(),
                serde_json::to_vec(&json!({"error": message})).unwrap_or_else(|_| b"{}".to_vec()),
            );
            let record = RunCatalogRecord::new(&run_id, RunStatus::Failed, "study");
            let _ = run_store.put(record, artifacts);
            fail_job(&jobs, &run_id, started, message);
            return;
        }
    };
    let Some(candidate_lake) = resolve_lake_data_dir() else {
        let message = "no lake data directory configured: set HARNESS_DATA_DIR or \
                       LAKE_LOCAL_DATA_DIR, or create .local/market-lake"
            .to_string();
        let mut artifacts = BTreeMap::new();
        artifacts.insert(
            "error.json".to_string(),
            serde_json::to_vec(&json!({"error": message})).unwrap_or_else(|_| b"{}".to_vec()),
        );
        let record = RunCatalogRecord::new(&run_id, RunStatus::Failed, "study");
        let _ = run_store.put(record, artifacts);
        fail_job(&jobs, &run_id, started, message);
        return;
    };
    let configuration_digest = configuration.configuration_hash();
    let mut configuration = configuration;
    let mut actual_data_start = None;
    let mut actual_data_end = None;
    if !configuration.symbols.is_empty() {
        if let Some(client) = LakeEnsureClient::from_env() {
            if let Err(message) = ensure_lake_data_with(&client, &configuration) {
                let mut artifacts = BTreeMap::new();
                artifacts.insert(
                    "error.json".to_string(),
                    serde_json::to_vec(&json!({"error": message}))
                        .unwrap_or_else(|_| b"{}".to_vec()),
                );
                let record = RunCatalogRecord::new(&run_id, RunStatus::Failed, "study");
                let _ = run_store.put(record, artifacts);
                fail_job(&jobs, &run_id, started, message);
                return;
            }
            match clamp_study_window(&client, &mut configuration) {
                Ok((start, end)) => {
                    actual_data_start = Some(start);
                    actual_data_end = Some(end);
                }
                Err(message) => {
                    let mut artifacts = BTreeMap::new();
                    artifacts.insert(
                        "error.json".to_string(),
                        serde_json::to_vec(&json!({"error": message}))
                            .unwrap_or_else(|_| b"{}".to_vec()),
                    );
                    let record = RunCatalogRecord::new(&run_id, RunStatus::Failed, "study");
                    let _ = run_store.put(record, artifacts);
                    fail_job(&jobs, &run_id, started, message);
                    return;
                }
            }
        }
    }
    match harness_strategy_jit::run_study_configuration(
        &configuration,
        &EngineOptions::default(),
        Some(output_dir.as_path()),
        candidate_lake.as_path(),
    ) {
        Ok((outcome, _artifact_path)) => {
            if let Ok(text) = fs::read(&config_path) {
                let _ = fs::write(output_dir.join("config.yaml"), text);
            }
            let mut artifacts = BTreeMap::new();
            collect_artifacts(&output_dir, &output_dir, &mut artifacts);
            if artifacts.is_empty() {
                artifacts.insert(
                    "study.json".to_string(),
                    outcome.artifact_json().into_bytes(),
                );
            }
            let mut record =
                RunCatalogRecord::new(&run_id, run_status(outcome.completed()), "study");
            record.configuration_digest = Some(configuration_digest);
            record.data_start = actual_data_start;
            record.data_end = actual_data_end;
            record.metrics = headline_metrics(&artifacts, &configuration);
            if let Err(error) = run_store.put(record, artifacts) {
                fail_job(&jobs, &run_id, started, error.to_string());
                return;
            }
            let mut job = mcp::store::JobRecord::new(
                &run_id,
                job_state(outcome.completed()),
                started,
                Utc::now(),
            );
            job.error_message = outcome.reason.clone();
            jobs.upsert(job);
        }
        Err(error) => {
            let message = error.to_string();
            let mut artifacts = BTreeMap::new();
            artifacts.insert(
                "error.json".to_string(),
                serde_json::to_vec(&json!({"error": message})).unwrap_or_else(|_| b"{}".to_vec()),
            );
            let mut record = RunCatalogRecord::new(&run_id, RunStatus::Failed, "study");
            record.configuration_digest = Some(configuration_digest);
            let _ = run_store.put(record, artifacts);
            fail_job(&jobs, &run_id, started, message);
        }
    }
}

/// Run one backtest on a detached worker and persist its complete result set.
///
/// Every file the native engine writes under `output_dir` is registered with the
/// run store keyed by its `/`-separated relative path, so `list_artifacts` and
/// `read_artifact` observe `backtest.json`, `metrics.json`, `report.html`, and
/// the recorded Parquet trail rather than only the canonical manifest.
fn run_background_backtest(
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    run_id: String,
    config_path: String,
    output_dir: PathBuf,
    started: DateTime<Utc>,
) {
    let guard = BackgroundJobGuard::new(jobs.clone(), &run_id, started);
    let outcome = catch_unwind(AssertUnwindSafe({
        let run_store = run_store.clone();
        let jobs = jobs.clone();
        let run_id = run_id.clone();
        let config_path = config_path.clone();
        let output_dir = output_dir.clone();
        move || {
            run_background_backtest_inner(
                run_store,
                jobs,
                run_id,
                config_path,
                output_dir,
                started,
            );
        }
    }));
    if let Err(payload) = outcome {
        fail_job(
            &jobs,
            &run_id,
            started,
            format!(
                "the background backtest panicked: {}",
                panic_message(payload.as_ref())
            ),
        );
    }
    drop(guard);
}

/// The backtest body. Every exit path writes a terminal job record.
fn run_background_backtest_inner(
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    run_id: String,
    config_path: String,
    output_dir: PathBuf,
    started: DateTime<Utc>,
) {
    let configuration = match backtest::backtest::job::load_backtest_config(Path::new(&config_path))
    {
        Ok(configuration) => configuration,
        Err(error) => {
            let message = error.to_string();
            let mut artifacts = BTreeMap::new();
            artifacts.insert(
                "error.json".to_string(),
                serde_json::to_vec(&json!({"error": message})).unwrap_or_else(|_| b"{}".to_vec()),
            );
            let record = RunCatalogRecord::new(&run_id, RunStatus::Failed, "");
            let _ = run_store.put(record, artifacts);
            fail_job(&jobs, &run_id, started, message);
            return;
        }
    };
    let Some(candidate_lake) = resolve_lake_data_dir() else {
        let message = "no lake data directory configured: set HARNESS_DATA_DIR or \
                       LAKE_LOCAL_DATA_DIR, or create .local/market-lake"
            .to_string();
        let mut artifacts = BTreeMap::new();
        artifacts.insert(
            "error.json".to_string(),
            serde_json::to_vec(&json!({"error": message})).unwrap_or_else(|_| b"{}".to_vec()),
        );
        let record = RunCatalogRecord::new(&run_id, RunStatus::Failed, strategy_id(&configuration));
        let _ = run_store.put(record, artifacts);
        fail_job(&jobs, &run_id, started, message);
        return;
    };
    if let Err(message) = ensure_lake_data(&configuration) {
        let mut artifacts = BTreeMap::new();
        artifacts.insert(
            "error.json".to_string(),
            serde_json::to_vec(&json!({"error": message})).unwrap_or_else(|_| b"{}".to_vec()),
        );
        let record = RunCatalogRecord::new(&run_id, RunStatus::Failed, strategy_id(&configuration));
        let _ = run_store.put(record, artifacts);
        fail_job(&jobs, &run_id, started, message);
        return;
    }
    match harness_strategy_jit::run_backtest_configuration(
        &configuration,
        &EngineOptions::default(),
        Some(output_dir.as_path()),
        candidate_lake.as_path(),
    ) {
        Ok((outcome, _artifact_path)) => {
            // Persist the configuration that produced the run beside the result
            // set so the run is reproducible and the document is retrievable.
            if let Ok(text) = fs::read(&config_path) {
                let _ = fs::write(output_dir.join("config.yaml"), text);
            }
            let mut artifacts = BTreeMap::new();
            collect_artifacts(&output_dir, &output_dir, &mut artifacts);
            if artifacts.is_empty() {
                artifacts.insert(
                    "backtest.json".to_string(),
                    outcome.artifact_json().into_bytes(),
                );
            }
            let mut record = RunCatalogRecord::new(
                &run_id,
                run_status(outcome.completed()),
                strategy_id(&configuration),
            );
            record.configuration_digest = Some(configuration.configuration_hash());
            record.metrics = headline_metrics(&artifacts, &configuration);
            if let Err(error) = run_store.put(record, artifacts) {
                fail_job(&jobs, &run_id, started, error.to_string());
                return;
            }
            let mut job = mcp::store::JobRecord::new(
                &run_id,
                job_state(outcome.completed()),
                started,
                Utc::now(),
            );
            job.error_message = outcome.reason.clone();
            jobs.upsert(job);
        }
        Err(error) => {
            let message = error.to_string();
            let mut artifacts = BTreeMap::new();
            artifacts.insert(
                "error.json".to_string(),
                serde_json::to_vec(&json!({"error": message})).unwrap_or_else(|_| b"{}".to_vec()),
            );
            let mut record =
                RunCatalogRecord::new(&run_id, RunStatus::Failed, strategy_id(&configuration));
            record.configuration_digest = Some(configuration.configuration_hash());
            let _ = run_store.put(record, artifacts);
            fail_job(&jobs, &run_id, started, message);
        }
    }
}

/// Ensure every configured symbol is fetched into the local lake before a run.
///
/// The market-lake process owns provider credentials and the actual fetch; the
/// harness only asks for the run window's symbols. A missing token or an empty
/// symbol set leaves the existing lake state unchanged.
pub(crate) fn ensure_lake_data(configuration: &BacktestConfig) -> Result<(), String> {
    let Some(client) = LakeEnsureClient::from_env() else {
        return Ok(());
    };
    ensure_lake_data_with(&client, configuration)
}

/// Resolve the local lake data directory the way every run path does: the
/// `HARNESS_DATA_DIR` or `LAKE_LOCAL_DATA_DIR` override, else the repository's
/// `.local/market-lake` when it exists.
pub(crate) fn resolve_lake_data_dir() -> Option<std::path::PathBuf> {
    let env_lake_dir = std::env::var("HARNESS_DATA_DIR")
        .or_else(|_| std::env::var("LAKE_LOCAL_DATA_DIR"))
        .ok()
        .map(std::path::PathBuf::from);
    let default_lake = std::path::PathBuf::from(".local").join("market-lake");
    env_lake_dir.or_else(|| {
        if default_lake.is_dir() {
            Some(default_lake)
        } else {
            None
        }
    })
}

/// Ensure every configured symbol with one already-built client.
///
/// Each symbol/data-type pair gets a single ensure request spanning the whole
/// run window: the lake plans and resumes its own fetches server-side, so a
/// full-history window is never split into ranges and a repeat request picks
/// up where an interrupted run left off. The requests are independent, so they
/// are issued concurrently; the run fails only when a request reports a
/// lake-side failure, naming what completed alongside what did not.
pub(crate) fn ensure_lake_data_with(
    client: &LakeEnsureClient,
    configuration: &BacktestConfig,
) -> Result<(), String> {
    if configuration.symbols.is_empty() {
        return Ok(());
    }
    let start = date_string(configuration.start_ns);
    let end = date_string(configuration.end_ns.saturating_sub(1));
    let mut requests = Vec::new();
    for symbol in &configuration.symbols {
        let (asset_class, lake_symbol) = lake_asset_class(symbol);
        for data_type in LAKE_DATA_TYPES {
            requests.push((asset_class, lake_symbol.clone(), data_type));
        }
    }
    let handles: Vec<_> = requests
        .into_iter()
        .map(|(asset_class, lake_symbol, data_type)| {
            let label = format!("{lake_symbol} {asset_class}/{data_type}");
            let client = client.clone();
            let start = start.clone();
            let end = end.clone();
            let handle = std::thread::spawn(move || {
                client.ensure(asset_class, data_type, &lake_symbol, &start, &end)
            });
            (label, handle)
        })
        .collect();
    let mut completed = Vec::new();
    let mut failures = Vec::new();
    for (label, handle) in handles {
        match handle.join() {
            Ok(Ok(())) => completed.push(label),
            Ok(Err(message)) => failures.push(message),
            Err(payload) => {
                failures.push(format!("{}: {}", label, panic_message(payload.as_ref())));
            }
        }
    }
    if failures.is_empty() {
        return Ok(());
    }
    let mut message = format!(
        "market-lake ensure failed for {} of {} requests",
        failures.len(),
        completed.len() + failures.len(),
    );
    if !completed.is_empty() {
        message.push_str(&format!(" (completed: {})", completed.join(", ")));
    }
    message.push_str("; ");
    message.push_str(&failures.join("; "));
    Err(message)
}

/// Clamp a study's data-window start to the lake's complete coverage.
///
/// Returns the actual `(start, end)` session dates the study will replay. The
/// start moves to the earliest session with complete data when the lake's
/// coverage begins after the requested start, so a model contract reaching
/// back before the provider's history no longer launches a study that can only
/// replay empty data. The requested window is kept when the lake reports no
/// complete session inside it.
fn clamp_study_window(
    client: &LakeEnsureClient,
    configuration: &mut BacktestConfig,
) -> Result<(NaiveDate, NaiveDate), String> {
    let earliest = earliest_complete_session_ns(
        client,
        &configuration.symbols,
        configuration.start_ns,
        configuration.end_ns,
    )?;
    if let Some(start_ns) = clamped_start_ns(configuration.start_ns, configuration.end_ns, earliest)
    {
        configuration.start_ns = start_ns;
    }
    let start_date = date_of(configuration.start_ns).ok_or_else(|| {
        "the clamped data window start is outside the representable range".to_string()
    })?;
    let end_date = date_of(configuration.end_ns.saturating_sub(1)).ok_or_else(|| {
        "the clamped data window end is outside the representable range".to_string()
    })?;
    Ok((start_date, end_date))
}

/// The earliest fully covered session across a symbol set, in epoch nanos.
///
/// Each symbol resolves to the earliest session where either lake data type is
/// complete, and the result is the latest of those per-symbol dates: the
/// study's data window starts only where every configured symbol carries data.
fn earliest_complete_session_ns(
    client: &LakeEnsureClient,
    symbols: &[String],
    start_ns: i64,
    end_ns: i64,
) -> Result<Option<i64>, String> {
    let start = date_string(start_ns);
    let end = date_string(end_ns.saturating_sub(1));
    let mut window_start: Option<i64> = None;
    for symbol in symbols {
        let (asset_class, lake_symbol) = lake_asset_class(symbol);
        let mut symbol_start: Option<i64> = None;
        for data_type in LAKE_DATA_TYPES {
            let Some(session) = client.earliest_complete_session(
                asset_class,
                data_type,
                &lake_symbol,
                &start,
                &end,
            )?
            else {
                continue;
            };
            let Some(session_ns) = date_ns(&session) else {
                continue;
            };
            symbol_start = Some(match symbol_start {
                Some(current) => current.min(session_ns),
                None => session_ns,
            });
        }
        if let Some(symbol_ns) = symbol_start {
            window_start = Some(match window_start {
                Some(current) => current.max(symbol_ns),
                None => symbol_ns,
            });
        }
    }
    Ok(window_start)
}

/// Move a window start to an earliest complete session without emptying it.
fn clamped_start_ns(requested_start_ns: i64, end_ns: i64, earliest: Option<i64>) -> Option<i64> {
    let start_ns = earliest?;
    if start_ns > requested_start_ns && start_ns < end_ns {
        Some(start_ns)
    } else {
        None
    }
}

/// Mark one background job failed when its worker exits without a terminal
/// record: an early return that forgot to record a state, or a panic.
struct BackgroundJobGuard {
    jobs: Arc<dyn JobRepository>,
    run_id: String,
    started: DateTime<Utc>,
}

impl BackgroundJobGuard {
    fn new(jobs: Arc<dyn JobRepository>, run_id: &str, started: DateTime<Utc>) -> Self {
        Self {
            jobs,
            run_id: run_id.to_string(),
            started,
        }
    }
}

impl Drop for BackgroundJobGuard {
    fn drop(&mut self) {
        let in_flight = self
            .jobs
            .get(&self.run_id)
            .is_some_and(|record| matches!(record.state, JobState::Queued | JobState::Running));
        if in_flight {
            fail_job(
                &self.jobs,
                &self.run_id,
                self.started,
                "the background run ended without recording a terminal state".to_string(),
            );
        }
    }
}

/// Render one panic payload as a message safe to persist on a job record.
fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(message) = payload.downcast_ref::<&str>() {
        return (*message).to_string();
    }
    if let Some(message) = payload.downcast_ref::<String>() {
        return message.clone();
    }
    "unknown panic".to_string()
}

fn lake_asset_class(symbol: &str) -> (&'static str, String) {
    match marketdata::crypto_pair(symbol) {
        Some(pair) => ("crypto", pair),
        None => ("equity", symbol.trim().to_uppercase()),
    }
}

fn date_string(nanos: i64) -> String {
    let seconds = nanos.div_euclid(1_000_000_000);
    chrono::DateTime::from_timestamp(seconds, 0)
        .map(|value| value.format("%Y-%m-%d").to_string())
        .unwrap_or_default()
}

/// Parse a `YYYY-MM-DD` session date into epoch nanoseconds at UTC midnight.
fn date_ns(value: &str) -> Option<i64> {
    let date = NaiveDate::parse_from_str(value.trim(), "%Y-%m-%d").ok()?;
    let seconds = date.and_hms_opt(0, 0, 0)?.and_utc().timestamp();
    seconds.checked_mul(1_000_000_000)
}

/// The UTC calendar date of an epoch-nanosecond timestamp.
fn date_of(nanos: i64) -> Option<NaiveDate> {
    let seconds = nanos.div_euclid(1_000_000_000);
    chrono::DateTime::from_timestamp(seconds, 0).map(|value| value.date_naive())
}

fn fail_job(jobs: &Arc<dyn JobRepository>, run_id: &str, started: DateTime<Utc>, message: String) {
    let mut job = mcp::store::JobRecord::new(run_id, JobState::Failed, started, Utc::now());
    job.error_message = Some(message);
    jobs.upsert(job);
}

/// Read every file under `directory` into `output` keyed by relative path.
fn collect_artifacts(base: &Path, directory: &Path, output: &mut BTreeMap<String, Vec<u8>>) {
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    let mut paths: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .collect();
    paths.sort();
    for path in paths {
        if path.is_dir() {
            collect_artifacts(base, &path, output);
        } else if let Ok(payload) = fs::read(&path) {
            if let Ok(relative) = path.strip_prefix(base) {
                output.insert(relative.to_string_lossy().replace('\\', "/"), payload);
            }
        }
    }
}

/// Extract the headline `metrics.json` fields into the catalog row.
fn headline_metrics(
    artifacts: &BTreeMap<String, Vec<u8>>,
    configuration: &BacktestConfig,
) -> BTreeMap<String, String> {
    let mut metrics = crate::http::launcher::artifact_metrics(artifacts);
    metrics.insert("strategy_id".to_string(), strategy_id(configuration));
    metrics
}

fn run_status(completed: bool) -> RunStatus {
    if completed {
        RunStatus::Complete
    } else {
        RunStatus::Failed
    }
}

fn job_state(completed: bool) -> JobState {
    if completed {
        JobState::Succeeded
    } else {
        JobState::Failed
    }
}

fn strategy_id(configuration: &BacktestConfig) -> String {
    configuration
        .strategy_instances
        .iter()
        .map(|instance| instance.instance_id.clone())
        .collect::<Vec<_>>()
        .join(",")
}

#[cfg(test)]
#[path = "../tests/unit/local.rs"]
mod tests;
