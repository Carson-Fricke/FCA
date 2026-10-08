//! Screening job launching for the harness HTTP boundary.
//!
//! `POST /v1/screeners/jobs` validates one `shell: screen` configuration,
//! admits an authoritative job, and hands the work to an injected
//! [`ScreenLauncher`]. The default [`InProcessScreenLauncher`] runs the injected
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
use super::store::{JobKind, JobRecord, JobState, JobStore};

/// Work description passed to an injected screening launcher.
#[derive(Debug, Clone, PartialEq)]
pub struct ScreenLaunchJob {
    /// Job identifier.
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
    /// Complete immutable result set keyed by job-relative artifact name.
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
    jobs: Arc<dyn JobStore>,
    runner: Arc<dyn ScreeningRunner>,
    clock: Arc<dyn Clock>,
}

impl InProcessScreenLauncher {
    /// Compose an in-process launcher over the injected boundaries.
    #[must_use]
    pub fn new(
        jobs: Arc<dyn JobStore>,
        runner: Arc<dyn ScreeningRunner>,
        clock: Arc<dyn Clock>,
    ) -> Self {
        Self {
            jobs,
            runner,
            clock,
        }
    }
}

impl ScreenLauncher for InProcessScreenLauncher {
    fn launch(&self, job: &ScreenLaunchJob) -> Result<(), LaunchError> {
        let started = self.clock.now();
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
                    Ok(Ok(outcome)) => finish_run(&jobs, &job_id, &configuration, outcome, started),
                    Ok(Err(message)) => fail_run(&jobs, &job_id, &configuration, started, message),
                    Err(payload) => fail_run(
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
    jobs: &Arc<dyn JobStore>,
    job_id: &str,
    configuration: &ScreenConfig,
    outcome: ScreenRunOutcome,
    started: DateTime<Utc>,
) {
    let Some(mut record) = result_record(
        jobs,
        job_id,
        configuration,
        if outcome.completed {
            JobState::Succeeded
        } else {
            JobState::Failed
        },
        outcome.rows,
        started,
    ) else {
        return;
    };
    record.error_message = outcome.error;
    if let Err(error) = jobs.put_result(record, outcome.artifacts) {
        fail_job(jobs, job_id, started, error.to_string());
    }
}

/// Record one screening run whose worker could not produce a result set.
fn fail_run(
    jobs: &Arc<dyn JobStore>,
    job_id: &str,
    configuration: &ScreenConfig,
    started: DateTime<Utc>,
    message: String,
) {
    let Some(mut record) = result_record(jobs, job_id, configuration, JobState::Failed, 0, started)
    else {
        return;
    };
    record.error_message = Some(message);
    let _ = jobs.put_result(record, BTreeMap::new());
}

/// Build the authoritative job record for one screening terminal state.
fn result_record(
    jobs: &Arc<dyn JobStore>,
    job_id: &str,
    configuration: &ScreenConfig,
    state: JobState,
    rows: u64,
    started: DateTime<Utc>,
) -> Option<JobRecord> {
    let mut record = jobs.get(job_id)?;
    record.kind = JobKind::Screen;
    record.state = state;
    record.updated_at = Utc::now().max(started);
    record.strategy_id = configuration.definition_id.clone();
    record.data_start = parse_date(&configuration.window_start());
    record.data_end = parse_date(&configuration.window_end());
    record.configuration_digest = Some(configuration.configuration_digest());
    record.metrics = screen_metrics(configuration, rows);
    Some(record)
}

/// Extract the headline screening fields into the job metrics.
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

fn parse_date(value: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(value, "%Y-%m-%d").ok()
}

fn fail_job(jobs: &Arc<dyn JobStore>, job_id: &str, started: DateTime<Utc>, message: String) {
    let Some(mut record) = jobs.get(job_id) else {
        return;
    };
    record.state = JobState::Failed;
    record.updated_at = Utc::now().max(started);
    record.error_message = Some(message);
    let _ = jobs.put_result(record, BTreeMap::new());
}

/// Mark one background job failed when its worker exits without a terminal
/// record: an early return that forgot to record a state, or a panic.
struct ScreenJobGuard {
    jobs: Arc<dyn JobStore>,
    job_id: String,
    started: DateTime<Utc>,
}

impl ScreenJobGuard {
    fn new(jobs: Arc<dyn JobStore>, job_id: &str, started: DateTime<Utc>) -> Self {
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
