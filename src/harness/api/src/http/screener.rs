//! Screening job launching for the harness HTTP boundary.
//!
//! `POST /v1/screeners/jobs` validates one `shell: screen` configuration,
//! records a queued job, and hands the work to an injected [`ScreenLauncher`].
//! The default [`InProcessScreenLauncher`] runs the injected
//! [`ScreeningRunner`] on a detached worker so the async route never blocks;
//! a job guard fails the job if the worker exits without recording a terminal
//! state, mirroring the MCP study submitter.

use std::collections::BTreeMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::sync::Arc;

use backtest::backtest::screen::ScreenConfig;
use chrono::{DateTime, NaiveDate, Utc};

use super::clock::Clock;
use super::launcher::LaunchError;
use super::store::{JobRecord, JobRepository, JobState, RunCatalogRecord, RunStatus, RunStore};

/// Work description passed to an injected screening launcher.
#[derive(Debug, Clone, PartialEq)]
pub struct ScreenLaunchJob {
    /// Job identifier (equal to the configuration's derived run ID).
    pub job_id: String,
    /// Validated `shell: screen` configuration.
    pub configuration: ScreenConfig,
}

/// The outcome of one screening run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScreenRunOutcome {
    /// Whether the run completed successfully.
    pub completed: bool,
    /// Number of ranked rows.
    pub rows: u64,
    /// Complete immutable result set keyed by run-relative artifact name.
    pub artifacts: BTreeMap<String, Vec<u8>>,
    /// Failure reason, present when the run did not complete.
    pub error: Option<String>,
}

/// Execution seam between the launch route and the screening run path.
pub trait ScreeningRunner: Send + Sync {
    /// Run one screening configuration, writing artifacts under `output_dir`
    /// when supplied.
    ///
    /// # Errors
    ///
    /// Returns a reason string when no run artifact could be produced at all.
    fn run(
        &self,
        configuration: &ScreenConfig,
        output_dir: Option<&Path>,
    ) -> Result<ScreenRunOutcome, String>;
}

/// Boundary that launches one validated screening configuration.
pub trait ScreenLauncher: Send + Sync {
    /// Launch one screening job without blocking the caller.
    ///
    /// # Errors
    ///
    /// Returns [`LaunchError`] when the worker cannot be started.
    fn launch(&self, job: &ScreenLaunchJob) -> Result<(), LaunchError>;
}

/// Runner that fails every screening run because no data source is composed.
///
/// Used where the native screening composition is not wanted (for example
/// in-memory test apps that never launch screens); the message names the
/// missing lake composition.
#[derive(Debug, Clone, Copy, Default)]
pub struct UnavailableScreeningRunner;

impl ScreeningRunner for UnavailableScreeningRunner {
    fn run(
        &self,
        _configuration: &ScreenConfig,
        _output_dir: Option<&Path>,
    ) -> Result<ScreenRunOutcome, String> {
        Err("the market-lake screening data source is not composed for this deployment".to_string())
    }
}

/// Launcher that runs one screening job on a detached worker in this process.
pub struct InProcessScreenLauncher {
    run_store: Arc<dyn RunStore>,
    jobs: Arc<dyn JobRepository>,
    runner: Arc<dyn ScreeningRunner>,
    clock: Arc<dyn Clock>,
}

impl InProcessScreenLauncher {
    /// Compose an in-process launcher over the injected boundaries.
    #[must_use]
    pub fn new(
        run_store: Arc<dyn RunStore>,
        jobs: Arc<dyn JobRepository>,
        runner: Arc<dyn ScreeningRunner>,
        clock: Arc<dyn Clock>,
    ) -> Self {
        Self {
            run_store,
            jobs,
            runner,
            clock,
        }
    }
}

impl ScreenLauncher for InProcessScreenLauncher {
    fn launch(&self, job: &ScreenLaunchJob) -> Result<(), LaunchError> {
        let started = self.clock.now();
        self.jobs.upsert(JobRecord::new(
            &job.job_id,
            JobState::Running,
            started,
            started,
        ));
        let run_store = self.run_store.clone();
        let jobs = self.jobs.clone();
        let runner = self.runner.clone();
        let job_id = job.job_id.clone();
        let configuration = job.configuration.clone();
        std::thread::Builder::new()
            .name(format!("screen-job-{}", job.job_id))
            .spawn(move || {
                let guard = ScreenJobGuard::new(jobs.clone(), &job_id, started);
                let result = catch_unwind(AssertUnwindSafe(|| runner.run(&configuration, None)));
                match result {
                    Ok(Ok(outcome)) => {
                        finish_run(&run_store, &jobs, &job_id, &configuration, outcome, started)
                    }
                    Ok(Err(message)) => {
                        fail_run(&run_store, &jobs, &job_id, &configuration, started, message)
                    }
                    Err(payload) => fail_run(
                        &run_store,
                        &jobs,
                        &job_id,
                        &configuration,
                        started,
                        format!(
                            "the background screening run panicked: {}",
                            panic_message(payload.as_ref())
                        ),
                    ),
                }
                drop(guard);
            })
            .map_err(|error| {
                LaunchError::Engine(format!("could not start the screening worker: {error}"))
            })?;
        Ok(())
    }
}

/// Record one completed or domain-failed screening run.
fn finish_run(
    run_store: &Arc<dyn RunStore>,
    jobs: &Arc<dyn JobRepository>,
    job_id: &str,
    configuration: &ScreenConfig,
    outcome: ScreenRunOutcome,
    started: DateTime<Utc>,
) {
    let status = if outcome.completed {
        RunStatus::Complete
    } else {
        RunStatus::Failed
    };
    let mut artifacts = outcome.artifacts;
    if artifacts.is_empty() {
        artifacts = error_artifact(
            outcome
                .error
                .as_deref()
                .unwrap_or("the screening run produced no artifacts"),
        );
    }
    let record = screen_record(job_id, configuration, status, outcome.rows);
    if let Err(error) = run_store.put(record, artifacts) {
        fail_job(jobs, job_id, started, error.to_string());
        return;
    }
    let mut job = job_record(job_id, job_state(outcome.completed), started);
    job.error_message = outcome.error;
    jobs.upsert(job);
}

/// Record one screening run whose worker could not produce a result set.
fn fail_run(
    run_store: &Arc<dyn RunStore>,
    jobs: &Arc<dyn JobRepository>,
    job_id: &str,
    configuration: &ScreenConfig,
    started: DateTime<Utc>,
    message: String,
) {
    let record = screen_record(job_id, configuration, RunStatus::Failed, 0);
    let _ = run_store.put(record, error_artifact(&message));
    fail_job(jobs, job_id, started, message);
}

/// Build the results-store catalog row for one screening run.
fn screen_record(
    job_id: &str,
    configuration: &ScreenConfig,
    status: RunStatus,
    rows: u64,
) -> RunCatalogRecord {
    let mut record = RunCatalogRecord::new(job_id, status, &configuration.definition_id);
    record.configuration_digest = Some(configuration.configuration_digest());
    record.data_start = parse_date(&configuration.window_start());
    record.data_end = parse_date(&configuration.window_end());
    record.metrics = BTreeMap::from([
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
    ]);
    record
}

fn parse_date(value: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(value, "%Y-%m-%d").ok()
}

fn error_artifact(message: &str) -> BTreeMap<String, Vec<u8>> {
    let mut artifacts = BTreeMap::new();
    artifacts.insert(
        "error.json".to_string(),
        serde_json::to_vec(&serde_json::json!({ "error": message }))
            .unwrap_or_else(|_| b"{}".to_vec()),
    );
    artifacts
}

fn job_record(job_id: &str, state: JobState, started: DateTime<Utc>) -> JobRecord {
    JobRecord::new(job_id, state, started, Utc::now())
}

fn fail_job(jobs: &Arc<dyn JobRepository>, job_id: &str, started: DateTime<Utc>, message: String) {
    let mut record = job_record(job_id, JobState::Failed, started);
    record.error_message = Some(message);
    jobs.upsert(record);
}

fn job_state(completed: bool) -> JobState {
    if completed {
        JobState::Succeeded
    } else {
        JobState::Failed
    }
}

/// Mark one background job failed when its worker exits without a terminal
/// record: an early return that forgot to record a state, or a panic.
struct ScreenJobGuard {
    jobs: Arc<dyn JobRepository>,
    job_id: String,
    started: DateTime<Utc>,
}

impl ScreenJobGuard {
    fn new(jobs: Arc<dyn JobRepository>, job_id: &str, started: DateTime<Utc>) -> Self {
        Self {
            jobs,
            job_id: job_id.to_string(),
            started,
        }
    }
}

impl Drop for ScreenJobGuard {
    fn drop(&mut self) {
        let in_flight = self
            .jobs
            .get(&self.job_id)
            .is_some_and(|record| matches!(record.state, JobState::Queued | JobState::Running));
        if in_flight {
            fail_job(
                &self.jobs,
                &self.job_id,
                self.started,
                "the background screening run ended without recording a terminal state".to_string(),
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
