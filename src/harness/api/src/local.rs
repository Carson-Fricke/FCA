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
use backtest::backtest::screen::ScreenConfig;

use crate::http::screener::ScreeningRunner;
use harness_lakeclient::BlockingLakeClient;
use mcp::store::{JobRepository, JobState, RunCatalogRecord, RunStatus, RunStore};

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
            "study_id": run_id.clone(),
            "job_id": run_id.clone(),
            "run_id": run_id,
            "state": "running",
            "kind": "study",
        }))
    }
}

/// Local screening submitter that runs the injected screening runner and
/// records the run.
pub struct NativeScreeningSubmitter {
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    runner: Arc<dyn ScreeningRunner>,
}

impl NativeScreeningSubmitter {
    /// Compose a submitter over the shared durable store and runner.
    #[must_use]
    pub fn new(
        run_store: Arc<dyn RunStore>,
        jobs: Arc<dyn JobRepository>,
        runner: Arc<dyn ScreeningRunner>,
    ) -> Self {
        Self {
            run_store,
            jobs,
            runner,
        }
    }
}

impl mcp::tools::ScreeningSubmitter for NativeScreeningSubmitter {
    fn submit(&self, config_path: &str, workspace: Option<&str>) -> Result<Value, String> {
        let configuration = backtest::backtest::job::load_screen_config(Path::new(config_path))
            .map_err(|error| error.to_string())?;
        let run_id = configuration.run_id();
        let started = Utc::now();
        self.jobs.upsert(mcp::store::JobRecord::new(
            &run_id,
            JobState::Running,
            started,
            started,
        ));
        let output_dir = workspace.map(PathBuf::from).unwrap_or_else(|| {
            std::env::temp_dir()
                .join("harness-mcp-screens")
                .join(&run_id)
        });
        let run_store = self.run_store.clone();
        let jobs = self.jobs.clone();
        let runner = self.runner.clone();
        let background_run_id = run_id.clone();
        let _handle = std::thread::spawn(move || {
            run_background_screen(
                run_store,
                jobs,
                runner,
                background_run_id,
                configuration,
                output_dir,
                started,
            );
        });
        Ok(json!({
            "boundary": "local",
            "run_id": run_id,
            "state": "running",
            "kind": "screening",
        }))
    }
}

/// Local program validator that compiles, loads, and constructs without a replay.
pub struct NativeStudyValidator;

#[cfg(test)]
#[path = "../tests/unit/local_validation.rs"]
mod validation_tests;

/// Build the runtime study job document from one authored model document.
///
/// The model's fields are exactly what the agent drafts; everything the
/// runtime job needs beyond them (the study shell, run id, JIT instance,
/// bundle hash) is derived here, so no impossible field is ever part of the
/// tool contract.
///
/// # Errors
/// Returns a reason when the authored source or harness contract is malformed.
pub fn authored_study_document(model: &Value) -> Result<Value, String> {
    authored_program_document(model, "study")
}

/// Derive a strategy job using the same authored model/configuration shape as studies.
pub fn authored_strategy_document(model: &Value) -> Result<Value, String> {
    authored_program_document(model, "backtest")
}

fn authored_program_document(model: &Value, shell: &str) -> Result<Value, String> {
    let object = model
        .as_object()
        .ok_or_else(|| "model must be an object".to_string())?;
    let raw_source = object
        .get("source")
        .and_then(Value::as_str)
        .filter(|text| !text.trim().is_empty())
        .ok_or_else(|| "model.source must be a non-empty string".to_string())?;
    // The JIT seam normalizes CRLF before hashing, so the derived bundle hash
    // and the compiled source must use the same normalized text.
    let source = raw_source.replace("\r\n", "\n");
    let configuration = object
        .get("harnessConfiguration")
        .and_then(Value::as_object)
        .ok_or_else(|| "model.harnessConfiguration must be an object".to_string())?;
    let mut data = match configuration.get("data") {
        Some(value) => value
            .as_object()
            .ok_or_else(|| "model.harnessConfiguration.data must be an object".to_string())?
            .clone(),
        None => serde_json::Map::new(),
    };
    let mut subscriptions = None;
    let mut subscription_echo = None;
    for value in [
        configuration.get("subscriptions"),
        data.get("subscriptions"),
    ]
    .into_iter()
    .flatten()
    {
        let inputs = crate::http::configuration::parse_subscriptions(value)
            .map_err(|error| error.to_string())?;
        if subscriptions
            .as_ref()
            .is_some_and(|declared| declared != &inputs)
        {
            return Err(
                "model.harnessConfiguration.data.subscriptions conflicts with subscriptions"
                    .to_string(),
            );
        }
        if subscription_echo.is_none() {
            subscription_echo = Some(value.clone());
        }
        subscriptions = Some(inputs);
    }
    if configuration.contains_key("interval") || data.contains_key("bar_interval") {
        return Err(
            "model.harnessConfiguration.interval is not accepted; declare subscriptions"
                .to_string(),
        );
    }
    let Some(inputs) = subscriptions.as_ref() else {
        return Err("model.harnessConfiguration.subscriptions is required".to_string());
    };
    let mut explicit_symbols = None;
    for value in [
        configuration.get("symbols"),
        configuration.get("requiredSymbols"),
        configuration.get("required_symbols"),
        data.get("symbols"),
    ]
    .into_iter()
    .flatten()
    {
        let items = value
            .as_array()
            .ok_or_else(|| "model.harnessConfiguration.symbols must be an array".to_string())?;
        let mut symbols = items
            .iter()
            .map(|item| {
                let symbol = item.as_str().ok_or_else(|| {
                    "model.harnessConfiguration.symbols entries must be strings".to_string()
                })?;
                if symbol.is_empty() || symbol.trim() != symbol {
                    return Err(
                        "model.harnessConfiguration.symbols entries must be nonempty and unpadded"
                            .to_string(),
                    );
                }
                Ok(symbol.to_string())
            })
            .collect::<Result<Vec<String>, String>>()?;
        symbols.sort();
        symbols.dedup();
        if symbols.is_empty() {
            return Err(
                "model.harnessConfiguration explicit symbols must not be empty".to_string(),
            );
        }
        if explicit_symbols
            .as_ref()
            .is_some_and(|declared| declared != &symbols)
        {
            return Err(
                "model.harnessConfiguration symbol aliases and data.symbols must match".to_string(),
            );
        }
        explicit_symbols = Some(symbols);
    }
    let start = configuration
        .get("start")
        .and_then(Value::as_str)
        .filter(|text| !text.trim().is_empty())
        .ok_or_else(|| {
            "model.harnessConfiguration.start must be a non-empty RFC 3339 string".to_string()
        })?;
    let end = configuration
        .get("end")
        .and_then(Value::as_str)
        .filter(|text| !text.trim().is_empty())
        .ok_or_else(|| {
            "model.harnessConfiguration.end must be a non-empty RFC 3339 string".to_string()
        })?;
    let mut symbols: Vec<String> = inputs
        .iter()
        .flat_map(|input| input.symbols.clone())
        .collect();
    symbols.sort();
    symbols.dedup();
    if symbols.is_empty() {
        return Err("model.harnessConfiguration.subscriptions must request symbols".to_string());
    }
    if explicit_symbols
        .as_ref()
        .is_some_and(|declared| declared != &symbols)
    {
        return Err(
            "model.harnessConfiguration.symbols must match the subscription symbol union"
                .to_string(),
        );
    }
    let digest = backtest::sha256::digest_hex(source.as_bytes());
    let run_id = format!("validate-{shell}-{}", &digest[..16]);
    data.insert("symbols".to_string(), json!(&symbols));
    if let Some(echo) = subscription_echo {
        // Preserve the authored echo's exact tagged shape; normalization is validation only.
        data.insert("subscriptions".to_string(), echo);
    }
    let mut document = configuration.clone();
    for key in [
        "interval",
        "subscriptions",
        "symbols",
        "requiredSymbols",
        "required_symbols",
    ] {
        document.remove(key);
    }
    document.insert("shell".to_string(), json!(shell));
    document.insert("run_id".to_string(), json!(&run_id));
    document.insert("start".to_string(), json!(start));
    document.insert("end".to_string(), json!(end));
    document.insert("data".to_string(), Value::Object(data));
    document.insert(
        "strategy_instances".to_string(),
        json!([{
            "instance_id": &run_id,
            "implementation": "jit",
            "bundle_hash": &digest,
            "capital_fraction": "1.0",
            "symbols": &symbols,
            "parameters": {"source": source},
        }]),
    );
    Ok(Value::Object(document))
}

impl mcp::tools::StudyValidator for NativeStudyValidator {
    fn sdk_reference(
        &self,
        arguments: &serde_json::Map<String, Value>,
    ) -> Result<Value, mcp::tools::ToolExecutionError> {
        harness_strategy_jit::validation_service()
            .sdk_reference(arguments)
            .map_err(|error| mcp::tools::ToolExecutionError::new(error.to_string()))
    }

    fn validate_strategy_result(
        &self,
        model: &Value,
    ) -> Result<Value, mcp::tools::ToolExecutionError> {
        validate_authored_program(model, "backtest")
    }

    fn validation_identity(&self) -> Option<String> {
        harness_strategy_jit::validation_service()
            .validation_identity()
            .ok()
    }

    fn validate(&self, model: &Value) -> Result<Value, String> {
        self.validate_result(model)
            .map_err(|error| error.to_string())
    }

    fn validate_result(&self, model: &Value) -> Result<Value, mcp::tools::ToolExecutionError> {
        validate_authored_program(model, "study")
    }
}

fn validate_authored_program(
    model: &Value,
    shell: &str,
) -> Result<Value, mcp::tools::ToolExecutionError> {
    use harness_strategy_jit::ValidationFailure;
    let kind = if shell == "study" {
        "study"
    } else {
        "strategy"
    };
    let cache_input = json!({"kind": kind, "model": model});
    harness_strategy_jit::validation_service()
        .validate(&cache_input, || {
            let document = authored_program_document(model, shell)
                .map_err(ValidationFailure::configuration)?;
            let configuration = if shell == "study" {
                backtest::backtest::job::parse_study_document(&document)
                    .map_err(|error| ValidationFailure::configuration(error.to_string()))?
            } else {
                BacktestConfig::from_yaml(&document.to_string())
                    .map_err(|error| ValidationFailure::configuration(error.to_string()))?
            };
            let mut result = harness_strategy_jit::inspect_configuration(&configuration)?;
            let sdk = harness_strategy_jit::validation_service()
                .sdk_reference(&serde_json::Map::new())
                .map_err(ValidationFailure::from)?;
            result["sdkIdentity"] = sdk["sdkIdentity"].clone();
            result["sdkVersion"] = sdk["sdkVersion"].clone();
            result["sdkAbiVersion"] = sdk["sdkAbiVersion"].clone();
            result["boundary"] = json!("local");
            Ok(result)
        })
        .map_err(|error| {
            mcp::tools::ToolExecutionError::with_payload(
                &error.message,
                json!({
                    "boundary": "local",
                    "valid": false,
                    "kind": kind,
                    "shell": shell,
                    "error": error.message,
                    "diagnostics": error.diagnostics,
                    "deterministicCompilerDiagnostic": error.deterministic_compiler_diagnostic,
                    "cacheable": error.cacheable,
                    "reused": error.reused,
                    "validationIdentity": error.validation_identity,
                }),
            )
        })
}

/// Run one screening job on a detached worker and persist its result set.
fn run_background_screen(
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    runner: Arc<dyn ScreeningRunner>,
    run_id: String,
    configuration: ScreenConfig,
    output_dir: PathBuf,
    started: DateTime<Utc>,
) {
    let guard = BackgroundJobGuard::new(jobs.clone(), &run_id, started);
    let outcome = catch_unwind(AssertUnwindSafe({
        let run_store = run_store.clone();
        let jobs = jobs.clone();
        let runner = runner.clone();
        let run_id = run_id.clone();
        let output_dir = output_dir.clone();
        move || {
            run_background_screen_inner(
                run_store,
                jobs,
                runner,
                run_id,
                configuration,
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
                "the background screening run panicked: {}",
                panic_message(payload.as_ref())
            ),
        );
    }
    drop(guard);
}

/// The screening body. Every exit path writes a terminal job record.
fn run_background_screen_inner(
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    runner: Arc<dyn ScreeningRunner>,
    run_id: String,
    configuration: ScreenConfig,
    output_dir: PathBuf,
    started: DateTime<Utc>,
) {
    let definition_id = configuration.definition_id.clone();
    let configuration_digest = configuration.configuration_digest();
    let data_start = date_of_text(&configuration.window_start());
    let data_end = date_of_text(&configuration.window_end());
    match runner.run(&configuration, Some(output_dir.as_path())) {
        Ok(outcome) => {
            let rows = outcome.rows;
            let mut artifacts = outcome.artifacts;
            if artifacts.is_empty() {
                artifacts.insert(
                    "error.json".to_string(),
                    serde_json::to_vec(&json!({
                        "error": outcome
                            .error
                            .clone()
                            .unwrap_or_else(|| "the screening run produced no artifacts".to_string())
                    }))
                    .unwrap_or_else(|_| b"{}".to_vec()),
                );
            }
            let mut record =
                RunCatalogRecord::new(&run_id, run_status(outcome.completed), &definition_id);
            record.configuration_digest = Some(configuration_digest);
            record.data_start = data_start;
            record.data_end = data_end;
            record.metrics = screen_metrics(&configuration, rows);
            if let Err(error) = run_store.put(record, artifacts) {
                fail_job(&jobs, &run_id, started, error.to_string());
                return;
            }
            let mut job = mcp::store::JobRecord::new(
                &run_id,
                job_state(outcome.completed),
                started,
                Utc::now(),
            );
            job.error_message = outcome.error;
            jobs.upsert(job);
        }
        Err(message) => {
            let mut artifacts = BTreeMap::new();
            artifacts.insert(
                "error.json".to_string(),
                serde_json::to_vec(&json!({"error": message})).unwrap_or_else(|_| b"{}".to_vec()),
            );
            let mut record = RunCatalogRecord::new(&run_id, RunStatus::Failed, &definition_id);
            record.configuration_digest = Some(configuration_digest);
            record.data_start = data_start;
            record.data_end = data_end;
            let _ = run_store.put(record, artifacts);
            fail_job(&jobs, &run_id, started, message);
        }
    }
}

/// Extract the headline screening fields into the catalog row.
fn screen_metrics(configuration: &ScreenConfig, rows: u64) -> BTreeMap<String, String> {
    BTreeMap::from([
        ("as_of".to_string(), configuration.as_of.clone()),
        (
            "bar_interval".to_string(),
            configuration.bar_interval.clone(),
        ),
        (
            "definition_id".to_string(),
            configuration.definition_id.clone(),
        ),
        ("rank_metric".to_string(), configuration.rank_metric.clone()),
        ("row_count".to_string(), rows.to_string()),
    ])
}

/// Parse a `YYYY-MM-DD` session date into a calendar date.
fn date_of_text(value: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(value, "%Y-%m-%d").ok()
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
    let client = match BlockingLakeClient::from_env() {
        Ok(client) => client,
        Err(error) => {
            let message = format!("market-lake client: {error}");
            record_failed_run(&run_store, &jobs, &run_id, "study", started, message);
            return;
        }
    };
    let configuration_digest = configuration.configuration_hash();
    let mut configuration = configuration;
    let mut actual_data_start = None;
    let mut actual_data_end = None;
    if !configuration.symbols.is_empty() {
        match clamp_study_window(&client, &mut configuration) {
            Ok((start, end)) => {
                actual_data_start = Some(start);
                actual_data_end = Some(end);
            }
            Err(message) => {
                record_failed_run(&run_store, &jobs, &run_id, "study", started, message);
                return;
            }
        }
    }
    match harness_strategy_jit::run_study_configuration(
        &configuration,
        &EngineOptions::default(),
        Some(output_dir.as_path()),
        &client,
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
    let client = match BlockingLakeClient::from_env() {
        Ok(client) => client,
        Err(error) => {
            let message = format!("market-lake client: {error}");
            record_failed_run(
                &run_store,
                &jobs,
                &run_id,
                &strategy_id(&configuration),
                started,
                message,
            );
            return;
        }
    };
    match harness_strategy_jit::run_backtest_configuration(
        &configuration,
        &EngineOptions::default(),
        Some(output_dir.as_path()),
        &client,
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

/// Record one pre-run failure and fail the job in one place.
fn record_failed_run(
    run_store: &Arc<dyn RunStore>,
    jobs: &Arc<dyn JobRepository>,
    run_id: &str,
    definition_id: &str,
    started: DateTime<Utc>,
    message: String,
) {
    let mut artifacts = BTreeMap::new();
    artifacts.insert(
        "error.json".to_string(),
        serde_json::to_vec(&json!({"error": message})).unwrap_or_else(|_| b"{}".to_vec()),
    );
    let record = RunCatalogRecord::new(run_id, RunStatus::Failed, definition_id);
    let _ = run_store.put(record, artifacts);
    fail_job(jobs, run_id, started, message);
}

/// Retain the operator's study window without acquiring implicit bar inputs.
///
/// Existing launchers call this seam before compilation. Acquisition belongs
/// to the engine after it resolves and validates the source subscription union.
///
/// # Errors
///
/// Returns a reason string when a window date is outside the representable range.
pub(crate) fn clamp_study_window(
    _client: &BlockingLakeClient,
    configuration: &mut BacktestConfig,
) -> Result<(NaiveDate, NaiveDate), String> {
    let start_date = date_of(configuration.start_ns)
        .ok_or_else(|| "the data window start is outside the representable range".to_string())?;
    let end_date = date_of(configuration.end_ns.saturating_sub(1))
        .ok_or_else(|| "the data window end is outside the representable range".to_string())?;
    Ok((start_date, end_date))
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
