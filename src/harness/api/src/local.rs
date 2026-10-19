//! Local-mode submitters for the harness MCP server.
//!
//! Submission adapters freeze authored input and admit it into the shared
//! execution store before notifying finite workers. Worker-only adapters drive
//! the native engine and publish exact result files. Execution never reloads a
//! mutable caller configuration or runs inside an API request process.

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
use mcp::store::{Admission, JobKind, JobRecord, JobState, JobStore};

/// One submission receipt shared by every local surface.
fn receipt(record: &JobRecord) -> Value {
    json!({
        "boundary": "local",
        "job_id": record.job_id,
        "state": record.state.as_str(),
        "kind": record.kind.as_str(),
    })
}

/// The receipt for a job this submission created.
fn created_receipt(job_id: &str, kind: JobKind) -> Value {
    json!({
        "boundary": "local",
        "job_id": job_id,
        "state": JobState::Queued.as_str(),
        "kind": kind.as_str(),
    })
}

/// Admission adapter; execution belongs to the isolated worker pool.
pub struct NativeBacktestSubmitter {
    job_store: Arc<dyn JobStore>,
    wake: Arc<dyn crate::execution::Wake>,
}
impl NativeBacktestSubmitter {
    pub fn new(job_store: Arc<dyn JobStore>, wake: Arc<dyn crate::execution::Wake>) -> Self {
        Self { job_store, wake }
    }
}
impl mcp::tools::BacktestSubmitter for NativeBacktestSubmitter {
    fn submit(
        &self,
        path: &str,
        _workspace: Option<&str>,
        job_id: Option<&str>,
    ) -> Result<Value, String> {
        admit_replay(
            &self.job_store,
            &self.wake,
            path,
            job_id,
            "backtest",
            JobKind::Backtest,
        )
    }
}
pub struct NativeStudySubmitter {
    job_store: Arc<dyn JobStore>,
    wake: Arc<dyn crate::execution::Wake>,
}
impl NativeStudySubmitter {
    pub fn new(job_store: Arc<dyn JobStore>, wake: Arc<dyn crate::execution::Wake>) -> Self {
        Self { job_store, wake }
    }
}
impl mcp::tools::StudySubmitter for NativeStudySubmitter {
    fn submit(
        &self,
        path: &str,
        _workspace: Option<&str>,
        job_id: Option<&str>,
    ) -> Result<Value, String> {
        admit_replay(
            &self.job_store,
            &self.wake,
            path,
            job_id,
            "study",
            JobKind::Study,
        )
    }
}
fn admit_replay(
    store: &Arc<dyn JobStore>,
    wake: &Arc<dyn crate::execution::Wake>,
    path: &str,
    job_id: Option<&str>,
    shell: &str,
    kind: JobKind,
) -> Result<Value, String> {
    let record = prepare_replay(Path::new(path), job_id, shell, kind)?;
    let id = record.job_id.clone();
    match store.admit(record).map_err(|e| e.to_string())? {
        Admission::Joined(record) => return Ok(receipt(&record)),
        Admission::Created(_) => {}
    }
    if let Err(e) = wake.wake() {
        eprintln!("execution remains queued; capacity hint failed: {e}");
    }
    Ok(created_receipt(&id, kind))
}

pub(crate) fn prepare_replay(
    path: &Path,
    job_id: Option<&str>,
    shell: &str,
    kind: JobKind,
) -> Result<JobRecord, String> {
    let (configuration, accepted_input) =
        backtest::backtest::job::load_replay_submission(path, job_id, shell)
            .map_err(|e| e.to_string())?;
    let id = configuration.job_id.clone();
    let mut record = JobRecord::new(&id, kind, configuration.configuration_hash(), Utc::now());
    populate_replay_record(&mut record, &configuration);
    record.accepted_input = accepted_input;
    Ok(record)
}
pub struct NativeScreeningSubmitter {
    job_store: Arc<dyn JobStore>,
    wake: Arc<dyn crate::execution::Wake>,
}
impl NativeScreeningSubmitter {
    pub fn new(job_store: Arc<dyn JobStore>, wake: Arc<dyn crate::execution::Wake>) -> Self {
        Self { job_store, wake }
    }
}
impl mcp::tools::ScreeningSubmitter for NativeScreeningSubmitter {
    fn submit(
        &self,
        path: &str,
        _workspace: Option<&str>,
        job_id: Option<&str>,
    ) -> Result<Value, String> {
        let configuration =
            backtest::backtest::job::load_screen_config_with_job_id(Path::new(path), job_id)
                .map_err(|e| e.to_string())?;
        let id = configuration.job_id.clone();
        let mut record = JobRecord::new(
            &id,
            JobKind::Screen,
            configuration.configuration_digest(),
            Utc::now(),
        );
        populate_screen_record(&mut record, &configuration);
        record.accepted_input = configuration.submission_document();
        match self.job_store.admit(record).map_err(|e| e.to_string())? {
            Admission::Joined(record) => return Ok(receipt(&record)),
            Admission::Created(_) => {}
        }
        if let Err(e) = self.wake.wake() {
            eprintln!("execution remains queued; capacity hint failed: {e}");
        }
        Ok(created_receipt(&id, JobKind::Screen))
    }
}

/// Execute one already-admitted immutable input inside a finite worker process.
/// It never admits, allocates a replacement ID, or reopens caller files.
pub fn execute_admitted(
    store: Arc<dyn JobStore>,
    record: JobRecord,
    access: Arc<dyn harness_lakeclient::MarketAccess>,
) {
    let output = match tempfile::tempdir() {
        Ok(dir) => dir,
        Err(error) => {
            record_failed_run(&store, &record.job_id, error.to_string());
            return;
        }
    };
    let id = record.job_id.clone();
    let started = Utc::now();
    let result = (|| -> Result<(), String> {
        match record.kind {
            JobKind::Backtest | JobKind::Study => {
                let configuration = BacktestConfig::from_yaml(&record.accepted_input.to_string())
                    .map_err(|e| e.to_string())?;
                if configuration.job_id != id
                    || configuration.configuration_hash()
                        != record
                            .configuration_digest
                            .clone()
                            .ok_or("accepted configuration digest missing")?
                {
                    return Err("accepted replay identity mismatch".into());
                }
                if record.kind == JobKind::Study {
                    run_background_study(
                        store.clone(),
                        id.clone(),
                        configuration,
                        output.path().into(),
                        started,
                        Some(access),
                    );
                } else {
                    run_background_backtest(
                        store.clone(),
                        id.clone(),
                        configuration,
                        output.path().into(),
                        started,
                        Some(access),
                    );
                }
            }
            JobKind::Screen => {
                let configuration = ScreenConfig::from_json_value(&record.accepted_input)
                    .map_err(|e| e.to_string())?;
                if configuration.job_id != id {
                    return Err("accepted screen identity mismatch".into());
                }
                let runner = Arc::new(crate::http::native::NativeScreenRunner {
                    lake_client: Some(Arc::new(
                        BlockingLakeClient::from_access(access).map_err(|e| e.to_string())?,
                    )),
                    ..Default::default()
                });
                run_background_screen(
                    store.clone(),
                    runner,
                    id.clone(),
                    configuration,
                    output.path().into(),
                    started,
                );
            }
        }
        Ok(())
    })();
    if let Err(error) = result {
        record_failed_run(&store, &id, error);
    }
}

/// Populate the replay job fields shared by backtests and studies.
fn populate_replay_record(record: &mut JobRecord, configuration: &BacktestConfig) {
    record.strategy_id = strategy_id(configuration);
    record.data_start = date_of(configuration.start_ns);
    record.data_end = date_of(configuration.end_ns.saturating_sub(1));
    record.configuration_digest = Some(configuration.configuration_hash());
}

/// Populate the screening job fields.
fn populate_screen_record(record: &mut JobRecord, configuration: &ScreenConfig) {
    record.strategy_id = configuration.definition_id.clone();
    record.data_start = date_of_text(&configuration.window_start());
    record.data_end = date_of_text(&configuration.window_end());
    record.configuration_digest = Some(configuration.configuration_digest());
}

/// Local program validator that compiles, loads, and constructs without a replay.
pub struct NativeStudyValidator;

/// Build the runtime study job document from one authored model document.
///
/// The model's fields are exactly what the agent drafts; everything the
/// runtime job needs beyond them (the study shell, JIT instance,
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
    let instance_id = format!("validate-{shell}-{}", &digest[..16]);
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
        "job_id",
    ] {
        document.remove(key);
    }
    document.insert("shell".to_string(), json!(shell));
    document.insert("start".to_string(), json!(start));
    document.insert("end".to_string(), json!(end));
    document.insert("data".to_string(), Value::Object(data));
    document.insert(
        "strategy_instances".to_string(),
        json!([{
            "instance_id": &instance_id,
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
    cached_authored_validation(model, shell, || inspect_authored_program(model, shell))
}

pub(crate) fn inspect_authored_program(
    model: &Value,
    shell: &str,
) -> Result<Value, harness_strategy_jit::ValidationFailure> {
    use harness_strategy_jit::ValidationFailure;
    let mut document =
        authored_program_document(model, shell).map_err(ValidationFailure::configuration)?;
    backtest::backtest::job::overlay_job_id_json(
        &mut document,
        Some(backtest::backtest::job::VALIDATION_JOB_ID),
    )
    .map_err(|error| ValidationFailure::configuration(error.to_string()))?;
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
}

pub(crate) fn cached_authored_validation(
    model: &Value,
    shell: &str,
    inspect: impl FnOnce() -> Result<Value, harness_strategy_jit::ValidationFailure>,
) -> Result<Value, mcp::tools::ToolExecutionError> {
    let kind = if shell == "study" {
        "study"
    } else {
        "strategy"
    };
    let cache_input = json!({"kind": kind, "model": model});
    harness_strategy_jit::validation_service()
        .validate(&cache_input, inspect)
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
    job_store: Arc<dyn JobStore>,
    runner: Arc<dyn ScreeningRunner>,
    job_id: String,
    configuration: ScreenConfig,
    output_dir: PathBuf,
    started: DateTime<Utc>,
) {
    let guard = BackgroundJobGuard::new(job_store.clone(), &job_id, started);
    if !start_background_job(&job_store, &job_id) {
        return;
    }
    let outcome = catch_unwind(AssertUnwindSafe({
        let job_store = job_store.clone();
        let runner = runner.clone();
        let job_id = job_id.clone();
        let output_dir = output_dir.clone();
        move || {
            run_background_screen_inner(job_store, runner, job_id, configuration, output_dir);
        }
    }));
    if let Err(payload) = outcome {
        fail_job(
            &job_store,
            &job_id,
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
    job_store: Arc<dyn JobStore>,
    runner: Arc<dyn ScreeningRunner>,
    job_id: String,
    configuration: ScreenConfig,
    output_dir: PathBuf,
) {
    match runner.run(&configuration, Some(output_dir.as_path())) {
        Ok(outcome) => {
            let rows = outcome.rows;
            publish_result(
                &job_store,
                &job_id,
                outcome.completed,
                screen_metrics(&configuration, rows),
                None,
                outcome.error,
                outcome.artifacts,
            );
        }
        Err(message) => {
            publish_result(
                &job_store,
                &job_id,
                false,
                screen_metrics(&configuration, 0),
                None,
                Some(message),
                BTreeMap::new(),
            );
        }
    }
}

/// Extract the headline screening fields into the job metrics.
fn screen_metrics(configuration: &ScreenConfig, rows: u64) -> BTreeMap<String, String> {
    BTreeMap::from([
        ("as_of".to_string(), configuration.as_of.clone()),
        (
            "bar_intervals".to_string(),
            configuration.bar_intervals.join(","),
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
    job_store: Arc<dyn JobStore>,
    job_id: String,
    configuration: BacktestConfig,
    output_dir: PathBuf,
    started: DateTime<Utc>,
    access: Option<Arc<dyn harness_lakeclient::MarketAccess>>,
) {
    let guard = BackgroundJobGuard::new(job_store.clone(), &job_id, started);
    if !start_background_job(&job_store, &job_id) {
        return;
    }
    let outcome = catch_unwind(AssertUnwindSafe({
        let job_store = job_store.clone();
        let job_id = job_id.clone();
        let output_dir = output_dir.clone();
        move || {
            run_background_study_inner(job_store, job_id, configuration, output_dir, access);
        }
    }));
    if let Err(payload) = outcome {
        fail_job(
            &job_store,
            &job_id,
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
    job_store: Arc<dyn JobStore>,
    job_id: String,
    mut configuration: BacktestConfig,
    output_dir: PathBuf,
    access: Option<Arc<dyn harness_lakeclient::MarketAccess>>,
) {
    let client = match access.map_or_else(
        BlockingLakeClient::from_env,
        BlockingLakeClient::from_access,
    ) {
        Ok(client) => client,
        Err(error) => {
            let message = format!("market-lake client: {error}");
            record_failed_run(&job_store, &job_id, message);
            return;
        }
    };
    let client = client.with_progress(mcp::store::progress_reporter(
        job_store.clone(),
        job_id.clone(),
    ));
    let mut actual_data_start = None;
    let mut actual_data_end = None;
    if !configuration.symbols.is_empty() {
        match clamp_study_window(&client, &mut configuration) {
            Ok((start, end)) => {
                actual_data_start = Some(start);
                actual_data_end = Some(end);
            }
            Err(message) => {
                record_failed_run(&job_store, &job_id, message);
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
            let mut artifacts = BTreeMap::new();
            collect_artifact_paths(&output_dir, &output_dir, &mut artifacts);
            if artifacts.is_empty() {
                record_failed_run(
                    &job_store,
                    &job_id,
                    "execution produced no artifact files".into(),
                );
                return;
            }
            let metrics = headline_file_metrics(&artifacts, &configuration);
            publish_files(
                &job_store,
                &job_id,
                outcome.completed(),
                metrics,
                Some((actual_data_start, actual_data_end)),
                outcome.reason.clone(),
                artifacts,
            );
        }
        Err(error) => {
            record_failed_run(&job_store, &job_id, error.to_string());
        }
    }
}

/// Run one backtest on a detached worker and persist its complete result set.
///
/// Every file the native engine writes under `output_dir` is registered with the
/// job store keyed by its `/`-separated relative path, so `list_artifacts` and
/// `read_artifact` observe `backtest.json`, `metrics.json`, `report.html`, and
/// the recorded Parquet trail rather than only the canonical manifest.
fn run_background_backtest(
    job_store: Arc<dyn JobStore>,
    job_id: String,
    configuration: BacktestConfig,
    output_dir: PathBuf,
    started: DateTime<Utc>,
    access: Option<Arc<dyn harness_lakeclient::MarketAccess>>,
) {
    let guard = BackgroundJobGuard::new(job_store.clone(), &job_id, started);
    if !start_background_job(&job_store, &job_id) {
        return;
    }
    let outcome = catch_unwind(AssertUnwindSafe({
        let job_store = job_store.clone();
        let job_id = job_id.clone();
        let output_dir = output_dir.clone();
        move || {
            run_background_backtest_inner(job_store, job_id, configuration, output_dir, access);
        }
    }));
    if let Err(payload) = outcome {
        fail_job(
            &job_store,
            &job_id,
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
    job_store: Arc<dyn JobStore>,
    job_id: String,
    configuration: BacktestConfig,
    output_dir: PathBuf,
    access: Option<Arc<dyn harness_lakeclient::MarketAccess>>,
) {
    let client = match access.map_or_else(
        BlockingLakeClient::from_env,
        BlockingLakeClient::from_access,
    ) {
        Ok(client) => client,
        Err(error) => {
            let message = format!("market-lake client: {error}");
            record_failed_run(&job_store, &job_id, message);
            return;
        }
    };
    let client = client.with_progress(mcp::store::progress_reporter(
        job_store.clone(),
        job_id.clone(),
    ));
    match harness_strategy_jit::run_backtest_configuration(
        &configuration,
        &EngineOptions::default(),
        Some(output_dir.as_path()),
        &client,
    ) {
        Ok((outcome, _artifact_path)) => {
            let mut artifacts = BTreeMap::new();
            collect_artifact_paths(&output_dir, &output_dir, &mut artifacts);
            if artifacts.is_empty() {
                record_failed_run(
                    &job_store,
                    &job_id,
                    "execution produced no artifact files".into(),
                );
                return;
            }
            let metrics = headline_file_metrics(&artifacts, &configuration);
            publish_files(
                &job_store,
                &job_id,
                outcome.completed(),
                metrics,
                None,
                outcome.reason.clone(),
                artifacts,
            );
        }
        Err(error) => {
            record_failed_run(&job_store, &job_id, error.to_string());
        }
    }
}

/// Publish one terminal job result, preserving admitted provenance fields.
fn publish_result(
    job_store: &Arc<dyn JobStore>,
    job_id: &str,
    completed: bool,
    metrics: BTreeMap<String, String>,
    data_bounds: Option<(Option<NaiveDate>, Option<NaiveDate>)>,
    error_message: Option<String>,
    artifacts: BTreeMap<String, Vec<u8>>,
) -> bool {
    let Some(mut record) = job_store.get(job_id) else {
        return false;
    };
    record.state = if completed {
        JobState::Succeeded
    } else {
        JobState::Failed
    };
    record.updated_at = Utc::now();
    record.metrics = metrics;
    if let Some((data_start, data_end)) = data_bounds {
        record.data_start = data_start;
        record.data_end = data_end;
    }
    record.error_message = error_message;
    match job_store.put_result(record, artifacts) {
        Ok(_) => true,
        Err(error) => {
            fail_job(job_store, job_id, Utc::now(), error.to_string());
            false
        }
    }
}

/// Record one pre-run failure and fail the job in one place.
fn record_failed_run(job_store: &Arc<dyn JobStore>, job_id: &str, message: String) {
    fail_job(job_store, job_id, Utc::now(), message);
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

/// Persist worker start before acquisition, compilation, or execution.
fn start_background_job(job_store: &Arc<dyn JobStore>, job_id: &str) -> bool {
    match job_store.mark_running(job_id, Utc::now()) {
        Ok(_) => true,
        Err(error) => {
            fail_job(
                job_store,
                job_id,
                Utc::now(),
                format!("could not persist worker start: {error}"),
            );
            false
        }
    }
}

/// Mark one background job failed when its worker exits without a terminal
/// record: an early return that forgot to record a state, or a panic.
struct BackgroundJobGuard {
    job_store: Arc<dyn JobStore>,
    job_id: String,
    started: DateTime<Utc>,
}

impl BackgroundJobGuard {
    fn new(job_store: Arc<dyn JobStore>, job_id: &str, started: DateTime<Utc>) -> Self {
        Self {
            job_store,
            job_id: job_id.to_string(),
            started,
        }
    }
}

impl Drop for BackgroundJobGuard {
    fn drop(&mut self) {
        let in_flight = self
            .job_store
            .get(&self.job_id)
            .is_some_and(|record| matches!(record.state, JobState::Queued | JobState::Running));
        if in_flight {
            fail_job(
                &self.job_store,
                &self.job_id,
                self.started,
                "the background job ended without recording a terminal state".to_string(),
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

fn fail_job(job_store: &Arc<dyn JobStore>, job_id: &str, started: DateTime<Utc>, message: String) {
    let Some(mut record) = job_store.get(job_id) else {
        return;
    };
    record.state = JobState::Failed;
    record.updated_at = Utc::now().max(started);
    record.error_message = Some(message);
    let _ = job_store.put_result(record, BTreeMap::new());
}

/// Inventory sealed engine outputs without retaining their payloads.
fn collect_artifact_paths(base: &Path, directory: &Path, output: &mut BTreeMap<String, PathBuf>) {
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_artifact_paths(base, &path, output);
        } else if let Ok(relative) = path.strip_prefix(base) {
            output.insert(relative.to_string_lossy().replace('\\', "/"), path);
        }
    }
}
fn headline_file_metrics(
    files: &BTreeMap<String, PathBuf>,
    configuration: &BacktestConfig,
) -> BTreeMap<String, String> {
    let mut bytes = BTreeMap::new();
    if let Some(path) = files.get("metrics.json") {
        if let Ok(payload) = fs::read(path) {
            bytes.insert("metrics.json".into(), payload);
        }
    }
    headline_metrics(&bytes, configuration)
}
fn publish_files(
    store: &Arc<dyn JobStore>,
    id: &str,
    completed: bool,
    metrics: BTreeMap<String, String>,
    bounds: Option<(Option<NaiveDate>, Option<NaiveDate>)>,
    error: Option<String>,
    files: BTreeMap<String, PathBuf>,
) -> bool {
    let Some(mut record) = store.get(id) else {
        return false;
    };
    record.state = if completed {
        JobState::Succeeded
    } else {
        JobState::Failed
    };
    record.updated_at = Utc::now();
    record.metrics = metrics;
    record.error_message = error;
    if let Some((start, end)) = bounds {
        record.data_start = start;
        record.data_end = end;
    }
    match store.put_files(record, files) {
        Ok(_) => true,
        Err(error) => {
            fail_job(store, id, Utc::now(), error.to_string());
            false
        }
    }
}

/// Extract the headline `metrics.json` fields into the job metrics.
fn headline_metrics(
    artifacts: &BTreeMap<String, Vec<u8>>,
    configuration: &BacktestConfig,
) -> BTreeMap<String, String> {
    let mut metrics = crate::http::launcher::artifact_metrics(artifacts);
    metrics.insert("strategy_id".to_string(), strategy_id(configuration));
    metrics
}

fn strategy_id(configuration: &BacktestConfig) -> String {
    configuration
        .strategy_instances
        .iter()
        .map(|instance| instance.instance_id.clone())
        .collect::<Vec<_>>()
        .join(",")
}
