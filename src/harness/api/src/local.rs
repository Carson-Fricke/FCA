//! Local-mode submitters for the harness MCP server.
//!
//! These adapters are composed by [`crate::mcp_cli`] so that the MCP
//! `run_backtest` and `run_sweep` tools work in local mode. They drive the
//! native Rust engine through `harness-host`, persist every run and artifact
//! into the shared durable results store, and return the same payload shapes
//! the remote API would return.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::Utc;
use serde_json::{json, Value};

use backtest::backtest::config::BacktestConfig;
use backtest::canonical_json::JsonValue;
use backtest::sweeps::runner::{
    CatalogRecord, RunBlobStore, RunCatalog, SweepJobLauncher, SweepRunner,
};
use backtest::sweeps::spec::{SweepMode, SweepSpec};
use backtest::sweeps::RunStatus as SweepRunStatus;

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
        let run =
            host::backtest::run_backtest_config(&configuration, Some(output_dir.as_path()), None);
        let (outcome, artifact_path) = match run {
            Ok(result) => result,
            Err(error) => {
                let mut job =
                    mcp::store::JobRecord::new(&run_id, JobState::Failed, started, Utc::now());
                job.error_message = Some(error.to_string());
                self.jobs.upsert(job);
                return Err(error.to_string());
            }
        };
        let mut record = RunCatalogRecord::new(
            &run_id,
            run_status(outcome.completed()),
            strategy_id(&configuration),
        );
        record.configuration_digest = Some(configuration.configuration_hash());
        let mut artifacts = BTreeMap::new();
        artifacts.insert(
            "backtest.json".to_string(),
            outcome.artifact_json().into_bytes(),
        );
        self.run_store
            .put(record, artifacts)
            .map_err(|error| error.to_string())?;
        let mut job = mcp::store::JobRecord::new(
            &run_id,
            job_state(outcome.completed()),
            started,
            Utc::now(),
        );
        job.error_message = outcome.reason.clone();
        self.jobs.upsert(job);
        Ok(json!({
            "boundary": "local",
            "run_id": run_id,
            "status": outcome.status.value(),
            "manifest_hash": outcome.manifest.manifest_hash(),
            "artifact_digest": outcome.artifact_digest(),
            "fills": outcome.fills.len(),
            "artifact_path": artifact_path.display().to_string(),
        }))
    }
}

/// Local sweep submitter backed by the native sweeps runner.
pub struct NativeSweepSubmitter {
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    root: PathBuf,
}

impl NativeSweepSubmitter {
    /// Compose a submitter over the shared durable store and a record root.
    #[must_use]
    pub fn new(
        run_store: Arc<dyn RunStore>,
        jobs: Arc<dyn JobRepository>,
        root: impl Into<PathBuf>,
    ) -> Self {
        Self {
            run_store,
            jobs,
            root: root.into(),
        }
    }
}

impl mcp::tools::SweepSubmitter for NativeSweepSubmitter {
    fn submit(&self, spec_path: &str) -> Result<Value, String> {
        let text = fs::read_to_string(spec_path).map_err(|error| error.to_string())?;
        let document: Value = serde_json::from_str(&text).map_err(|error| error.to_string())?;
        let (spec, base) = parse_sweep_spec(&document)?;
        let points = spec.expand();
        let mut configs = BTreeMap::new();
        for point in &points {
            let mut config = base.clone();
            config.run_id = point.run_id.clone();
            if let Some(instance) = config
                .strategy_instances
                .iter_mut()
                .find(|instance| instance.instance_id == spec.instance_id)
            {
                for (name, value) in &point.parameters {
                    instance
                        .parameters
                        .insert(name.clone(), normalize_parameter(value));
                }
            }
            configs.insert(point.run_id.clone(), config);
        }
        let launcher = NativeSweepLauncher {
            configs,
            run_store: self.run_store.clone(),
            jobs: self.jobs.clone(),
            output_root: self.root.join("sweep-engine"),
        };
        let blobs = FileSweepBlobStore {
            root: self.root.join("sweeps"),
        };
        let catalog = StoreRunCatalog {
            store: self.run_store.clone(),
        };
        let mut runner = SweepRunner::new(Box::new(launcher), Box::new(blobs), Box::new(catalog));
        let submission = runner.submit(&spec)?;
        Ok(json!({
            "sweep_id": submission.sweep_id,
            "requested": submission.requested,
            "expanded": submission.expanded,
            "duplicates_skipped": submission.duplicates_skipped,
            "run_ids": submission.run_ids,
        }))
    }
}

fn parse_sweep_spec(document: &Value) -> Result<(SweepSpec, BacktestConfig), String> {
    let base_path = document
        .get("base_config_path")
        .and_then(Value::as_str)
        .ok_or_else(|| "sweep spec requires a 'base_config_path' string".to_string())?;
    let base = backtest::backtest::job::load_backtest_config(Path::new(base_path))
        .map_err(|error| error.to_string())?;
    let instance_id = document
        .get("instance_id")
        .and_then(Value::as_str)
        .ok_or_else(|| "sweep spec requires an 'instance_id' string".to_string())?
        .to_string();
    let mode = document
        .get("mode")
        .and_then(Value::as_str)
        .and_then(SweepMode::parse)
        .ok_or_else(|| "sweep spec mode must be 'grid' or 'random'".to_string())?;
    let space_object = document
        .get("space")
        .and_then(Value::as_object)
        .ok_or_else(|| "sweep spec requires a 'space' object".to_string())?;
    let mut space: BTreeMap<String, Vec<JsonValue>> = BTreeMap::new();
    for (name, values) in space_object {
        let values = values
            .as_array()
            .ok_or_else(|| format!("sweep parameter '{name}' must be an array"))?;
        space.insert(name.clone(), values.iter().map(to_canonical_json).collect());
    }
    let spec = match mode {
        SweepMode::Grid => SweepSpec::grid(base.clone(), instance_id, space),
        SweepMode::Random => {
            let count = document
                .get("count")
                .and_then(Value::as_u64)
                .ok_or_else(|| "random sweeps require an integer 'count'".to_string())?;
            let seed = document
                .get("seed")
                .and_then(Value::as_u64)
                .ok_or_else(|| "random sweeps require an integer 'seed'".to_string())?;
            SweepSpec::random(base.clone(), instance_id, space, count as usize, seed)
        }
    }
    .map_err(|error| error.to_string())?;
    Ok((spec, base))
}

fn to_canonical_json(value: &Value) -> JsonValue {
    match value {
        Value::Null => JsonValue::Null,
        Value::Bool(inner) => JsonValue::Bool(*inner),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                JsonValue::Int(integer)
            } else if let Some(unsigned) = number.as_u64() {
                JsonValue::UInt(unsigned)
            } else {
                JsonValue::object([("$decimal", JsonValue::String(number.to_string()))])
            }
        }
        Value::String(text) => JsonValue::String(text.clone()),
        Value::Array(items) => JsonValue::Array(items.iter().map(to_canonical_json).collect()),
        Value::Object(entries) => JsonValue::Object(
            entries
                .iter()
                .map(|(key, item)| (key.clone(), to_canonical_json(item)))
                .collect(),
        ),
    }
}

/// Convert a tagged `{"$decimal": ...}` level into a strategy-safe string.
fn normalize_parameter(value: &JsonValue) -> JsonValue {
    if let JsonValue::Object(entries) = value {
        if entries.len() == 1 {
            if let Some(JsonValue::String(text)) = entries.get("$decimal") {
                return JsonValue::String(text.clone());
            }
        }
    }
    value.clone()
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

struct StoreRunCatalog {
    store: Arc<dyn RunStore>,
}

impl RunCatalog for StoreRunCatalog {
    fn get(&self, run_id: &str) -> Option<CatalogRecord> {
        self.store.get(run_id).map(|record| CatalogRecord {
            run_id: record.run_id,
            status: match record.status {
                RunStatus::Queued => SweepRunStatus::Queued,
                RunStatus::Running => SweepRunStatus::Running,
                RunStatus::Complete => SweepRunStatus::Complete,
                RunStatus::Failed => SweepRunStatus::Failed,
            },
            metrics: record.metrics,
        })
    }
}

struct FileSweepBlobStore {
    root: PathBuf,
}

impl RunBlobStore for FileSweepBlobStore {
    fn replace(&mut self, relative_path: &str, payload: &[u8]) {
        let path = self.root.join(relative_path);
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let _ = fs::write(path, payload);
    }

    fn read(&self, relative_path: &str) -> Option<Vec<u8>> {
        fs::read(self.root.join(relative_path)).ok()
    }

    fn list_prefix(&self, prefix: &str) -> Vec<String> {
        let mut paths = Vec::new();
        collect_paths(&self.root, &self.root, &mut paths);
        paths.retain(|path| path.starts_with(prefix));
        paths.sort();
        paths
    }
}

fn collect_paths(root: &Path, directory: &Path, output: &mut Vec<String>) {
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
            collect_paths(root, &path, output);
        } else if let Ok(relative) = path.strip_prefix(root) {
            output.push(relative.to_string_lossy().replace('\\', "/"));
        }
    }
}

struct NativeSweepLauncher {
    configs: BTreeMap<String, BacktestConfig>,
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    output_root: PathBuf,
}

impl SweepJobLauncher for NativeSweepLauncher {
    fn launch(&self, run_id: &str) {
        let Some(config) = self.configs.get(run_id) else {
            return;
        };
        let started = Utc::now();
        self.jobs.upsert(mcp::store::JobRecord::new(
            run_id,
            JobState::Running,
            started,
            started,
        ));
        let output_dir = self.output_root.join(run_id);
        match host::backtest::run_backtest_config(config, Some(output_dir.as_path()), None) {
            Ok((outcome, _artifact_path)) => {
                let mut record = RunCatalogRecord::new(
                    run_id,
                    run_status(outcome.completed()),
                    strategy_id(config),
                );
                record.configuration_digest = Some(config.configuration_hash());
                let mut artifacts = BTreeMap::new();
                artifacts.insert(
                    "backtest.json".to_string(),
                    outcome.artifact_json().into_bytes(),
                );
                let _ = self.run_store.put(record, artifacts);
                let mut job = mcp::store::JobRecord::new(
                    run_id,
                    job_state(outcome.completed()),
                    started,
                    Utc::now(),
                );
                job.error_message = outcome.reason.clone();
                self.jobs.upsert(job);
            }
            Err(error) => {
                let message = error.to_string();
                let mut artifacts = BTreeMap::new();
                artifacts.insert(
                    "error.json".to_string(),
                    serde_json::to_vec(&json!({"error": message}))
                        .unwrap_or_else(|_| b"{}".to_vec()),
                );
                let record = RunCatalogRecord::new(run_id, RunStatus::Failed, strategy_id(config));
                let _ = self.run_store.put(record, artifacts);
                let mut job =
                    mcp::store::JobRecord::new(run_id, JobState::Failed, started, Utc::now());
                job.error_message = Some(message);
                self.jobs.upsert(job);
            }
        }
    }
}

#[cfg(test)]
#[path = "../tests/unit/local.rs"]
mod tests;
