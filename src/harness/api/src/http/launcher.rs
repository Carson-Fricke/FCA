//! Backtest job launching for the harness HTTP boundary.
//!
//! Mirrors `harness.http.launcher`: the launch route validates one submitted
//! configuration, records a queued job, and hands the work to an injected
//! [`BacktestLauncher`]. The default [`InProcessLauncher`] runs the injected
//! [`BacktestRunner`] inside the API process and records the completed run so
//! a local launch is immediately queryable.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

use super::clock::Clock;
use super::configuration::ValidatedConfiguration;
use serde_json::Value;

use super::store::{JobRecord, JobRepository, RunCatalogRecord, RunStatus, RunStore, StoreError};

/// Work description passed to an injected backtest launcher.
#[derive(Debug, Clone, PartialEq)]
pub struct LaunchJob {
    /// Job identifier (equal to the configuration's run ID).
    pub job_id: String,
    /// Validated submitted configuration.
    pub configuration: ValidatedConfiguration,
}

/// The outcome of one deterministic backtest run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunOutcome {
    /// Whether the run completed successfully.
    pub completed: bool,
    /// Complete immutable result set keyed by run-relative artifact name.
    pub artifacts: BTreeMap<String, Vec<u8>>,
}

/// Execution seam between the launch route and the Rust backtest engine.
pub trait BacktestRunner: Send + Sync {
    /// Run one backtest for an already-validated configuration.
    ///
    /// # Errors
    ///
    /// Returns a reason string when the engine cannot complete the run.
    fn run(&self, configuration: &ValidatedConfiguration) -> Result<RunOutcome, String>;
}

/// Default runner used until the Rust backtest engine is ported.
#[derive(Debug, Clone, Copy, Default)]
pub struct UnportedBacktestRunner;

impl BacktestRunner for UnportedBacktestRunner {
    fn run(&self, _configuration: &ValidatedConfiguration) -> Result<RunOutcome, String> {
        Err(
            "the Rust backtest engine is not yet ported; the Python harness remains authoritative"
                .to_string(),
        )
    }
}

/// A launcher failure surfaced back to the route as a failed job.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchError {
    /// The engine rejected or failed the run.
    Engine(String),
    /// Persisting the completed run failed.
    Store(String),
}

impl fmt::Display for LaunchError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Engine(reason) => formatter.write_str(reason),
            Self::Store(reason) => write!(formatter, "StoreError: {reason}"),
        }
    }
}

impl std::error::Error for LaunchError {}

impl From<StoreError> for LaunchError {
    fn from(error: StoreError) -> Self {
        Self::Store(error.to_string())
    }
}

/// Boundary that launches one validated backtest configuration.
pub trait BacktestLauncher: Send + Sync {
    /// Launch one backtest job.
    ///
    /// # Errors
    ///
    /// Returns [`LaunchError`] when the run cannot be completed.
    fn launch(&self, job: &LaunchJob) -> Result<(), LaunchError>;
}

/// Map one completed runner result onto a results-store catalog row.
#[must_use]
pub fn launch_record(
    configuration: &ValidatedConfiguration,
    outcome: &RunOutcome,
) -> RunCatalogRecord {
    let mut record = RunCatalogRecord::new(
        configuration.run_id.clone(),
        if outcome.completed {
            RunStatus::Complete
        } else {
            RunStatus::Failed
        },
        configuration.strategy_ids.join(","),
    );
    record.data_start = configuration.data_start;
    record.data_end = configuration.data_end;
    record.configuration_digest = Some(configuration.configuration_digest.clone());
    record.metrics = artifact_metrics(&outcome.artifacts);
    record
}

/// Extract built-in and custom scalar values from a run's `metrics.json`.
#[must_use]
pub fn artifact_metrics(artifacts: &BTreeMap<String, Vec<u8>>) -> BTreeMap<String, String> {
    let Some(payload) = artifacts.get("metrics.json") else {
        return BTreeMap::new();
    };
    let Ok(document) = serde_json::from_slice::<Value>(payload) else {
        return BTreeMap::new();
    };
    let mut metrics = BTreeMap::new();
    let equity = document.get("equity");
    let execution = document.get("execution");
    for (key, section) in [
        ("total_return", equity),
        ("max_drawdown", equity),
        ("cagr", equity),
        ("fill_count", execution),
        ("fees_total", execution),
    ] {
        if let Some(value) = section.and_then(|section| section.get(key)) {
            if let Some(text) = scalar_string(value) {
                metrics.insert(key.to_string(), text);
            }
        }
    }
    if let Some(custom) = document.get("custom").and_then(Value::as_object) {
        for (key, value) in custom {
            if let Some(text) = scalar_string(value) {
                metrics.entry(key.clone()).or_insert(text);
            }
        }
    }
    metrics
}

fn scalar_string(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(flag) => Some(flag.to_string()),
        _ => None,
    }
}

/// Persist one failed launch while preserving the original creation time.
pub fn mark_failed(jobs: &dyn JobRepository, job_id: &str, clock: &dyn Clock, error: &LaunchError) {
    let existing = jobs.get(job_id);
    let created_at = existing
        .as_ref()
        .map_or_else(|| clock.now(), |record| record.created_at);
    let mut record = JobRecord::new(
        job_id,
        super::store::JobState::Failed,
        created_at,
        clock.now(),
    );
    record.error_message = Some(error.to_string());
    jobs.upsert(record);
}

/// Launcher that runs one deterministic backtest inside this process.
pub struct InProcessLauncher {
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    runner: Arc<dyn BacktestRunner>,
    clock: Arc<dyn Clock>,
}

impl InProcessLauncher {
    /// Compose an in-process launcher over the injected boundaries.
    #[must_use]
    pub fn new(
        run_store: Arc<dyn RunStore>,
        jobs: Arc<dyn JobRepository>,
        runner: Arc<dyn BacktestRunner>,
        clock: Arc<dyn Clock>,
    ) -> Self {
        Self {
            run_store,
            jobs,
            runner,
            clock,
        }
    }

    fn update_job(&self, job_id: &str, state: super::store::JobState) {
        let existing = self.jobs.get(job_id);
        let created_at = existing
            .as_ref()
            .map_or_else(|| self.clock.now(), |record| record.created_at);
        self.jobs
            .upsert(JobRecord::new(job_id, state, created_at, self.clock.now()));
    }
}

impl BacktestLauncher for InProcessLauncher {
    fn launch(&self, job: &LaunchJob) -> Result<(), LaunchError> {
        self.update_job(&job.job_id, super::store::JobState::Running);
        let outcome = self
            .runner
            .run(&job.configuration)
            .map_err(LaunchError::Engine)?;
        let record = launch_record(&job.configuration, &outcome);
        self.run_store.put(record, outcome.artifacts.clone())?;
        self.update_job(&job.job_id, super::store::JobState::Succeeded);
        Ok(())
    }
}
