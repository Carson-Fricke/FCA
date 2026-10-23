//! Native backtest engine runner for the `backtest-job` command surface.
//!
//! [`NativeBacktestJobRunner`] plugs the Rust engine into the CLI seam
//! defined by [`crate::BacktestJobRunner`]. The engine core (clock, event
//! bus, replay, matching, fees, portfolio, risk, and results) is native Rust;
//! the configured strategies and studies are JIT-authored Rust programs
//! loaded through the `harness-strategy-jit` seam.
//!
//! Market data streams from the market-lake HTTP routes through
//! `harness-lakeclient`. The endpoint resolves from `LAKE_API_URL` /
//! `LAKE_API_BASE_URL` and the bearer token from `LAKE_API__BEARER_TOKEN`; a
//! run without a configured lake fails loudly and never falls back to local
//! files or a synthetic fixture.

use std::path::Path;
use std::path::PathBuf;

use backtest::backtest::engine_core::EngineOptions;
use backtest::backtest::outcome::BacktestOutcome;
use harness_lakeclient::BlockingLakeClient;

use crate::BacktestJobArgs;
use crate::BacktestJobError;
use crate::BacktestJobRunner;
use crate::BacktestRunSummary;
use crate::ScreenJobArgs;
use crate::ScreenJobError;
use crate::ScreenJobRunner;
use crate::ScreenRunSummary;
use crate::StudyJobArgs;
use crate::StudyJobRunner;

/// Build the market-lake client from the process environment.
///
/// The error names the missing configuration (`LAKE_API__BEARER_TOKEN`), so a
/// run with no lake URL/token fails loudly before any engine work.
fn require_lake_client() -> Result<BlockingLakeClient, BacktestJobError> {
    BlockingLakeClient::from_env()
        .map_err(|error| BacktestJobError::Engine(format!("market-lake client: {error}")))
}

/// Run `backtest-job` through the native Rust engine and JIT-authored strategy programs.
#[derive(Debug, Clone, Copy, Default)]
pub struct NativeBacktestJobRunner;

impl BacktestJobRunner for NativeBacktestJobRunner {
    fn run(&self, args: &BacktestJobArgs) -> Result<BacktestRunSummary, BacktestJobError> {
        let Some(config) = args.config.as_deref() else {
            return Err(BacktestJobError::Engine(
                "A backtest configuration is required: backtest-job --config <path>".to_string(),
            ));
        };
        let client = require_lake_client()?;
        let configuration =
            harness_research::configuration::load_backtest_config(Path::new(config))
                .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
        let (outcome, artifact_path) = harness_research::run_backtest_configuration(
            &configuration,
            &EngineOptions::default(),
            args.output_dir.as_deref(),
            &client,
        )
        .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
        Ok(summary_from_outcome(&outcome, artifact_path))
    }
}

/// Run `study-job` through the native Rust engine and JIT-authored study programs.
#[derive(Debug, Clone, Copy, Default)]
pub struct NativeStudyJobRunner;

impl StudyJobRunner for NativeStudyJobRunner {
    fn run(&self, args: &StudyJobArgs) -> Result<BacktestRunSummary, BacktestJobError> {
        let Some(config) = args.config.as_deref() else {
            return Err(BacktestJobError::Engine(
                "A study configuration is required: study-job --config <path>".to_string(),
            ));
        };
        let client = require_lake_client()?;
        let configuration = harness_research::configuration::load_study_config(config)
            .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
        if !configuration.symbols.is_empty() {
            crate::local::study_window_dates(&configuration).map_err(BacktestJobError::Engine)?;
        }
        let (outcome, artifact_path) = harness_research::run_study_configuration(
            &configuration,
            &EngineOptions::default(),
            args.output_dir.as_deref(),
            &client,
        )
        .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
        Ok(summary_from_outcome(&outcome, artifact_path))
    }
}

/// Project one native outcome onto the CLI's canonical stdout summary.
#[must_use]
fn summary_from_outcome(outcome: &BacktestOutcome, artifact_path: PathBuf) -> BacktestRunSummary {
    BacktestRunSummary {
        job_id: outcome.manifest.job_id.clone(),
        status: outcome.status.value().to_string(),
        manifest_hash: outcome.manifest.manifest_hash(),
        artifact_digest: outcome.artifact_digest(),
        outcome_digest: outcome.outcome_digest(),
        market_events: outcome.market_event_count,
        orders: outcome.order_count,
        fills: outcome.fills.len() as u64,
        artifact_path,
        reason: outcome.reason.clone(),
    }
}

/// Run `screen-job` through the JIT screening seam.
///
/// The data source is the real
/// [`LakeScreeningSource`](harness_research::LakeScreeningSource), reading
/// the market-lake HTTP routes. A missing or rejected lake configuration fails
/// the command loudly before any run; no synthetic data is ever substituted.
#[derive(Debug, Clone, Copy, Default)]
pub struct NativeScreenJobRunner;

impl ScreenJobRunner for NativeScreenJobRunner {
    fn run(&self, args: &ScreenJobArgs) -> Result<ScreenRunSummary, ScreenJobError> {
        let Some(config) = args.config.as_deref() else {
            return Err(ScreenJobError::Engine(
                "A screening configuration is required: screen-job --config <path>".to_string(),
            ));
        };
        let configuration = harness_research::configuration::load_screen_config(Path::new(config))
            .map_err(|error| ScreenJobError::Engine(error.to_string()))?;
        let source = harness_research::LakeScreeningSource::from_env()
            .map_err(|error| ScreenJobError::Engine(error.to_string()))?;
        let (outcome, artifact_path) = harness_research::run_screening_configuration(
            &configuration,
            &source,
            &harness_research::ScreenRunOptions::default(),
            args.output_dir.as_deref(),
        )
        .map_err(|error| ScreenJobError::Engine(error.to_string()))?;
        Ok(ScreenRunSummary {
            job_id: outcome.job_id.clone(),
            status: outcome.status.value().to_string(),
            rows: outcome.row_count() as u64,
            artifact_path,
            reason: outcome.error().map(str::to_string),
        })
    }
}
