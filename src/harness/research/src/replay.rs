//! Owns application composition from acquired inputs through replay and result publication.
//! Trading policy stays in trading; deterministic dispatch stays in backtest.
use std::path::Path;
use std::path::PathBuf;

use harness_contracts::progress::ExecutionProgress;
use harness_contracts::progress::ExecutionStage;
use harness_lakeclient::BlockingLakeClient;

use crate::acquisition::lake_fixture_with_subscriptions;
use crate::acquisition::lake_scale_options;
use crate::error::BacktestJobError;
use crate::publishing::write_run_artifacts;
use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::run_native_engine;
use backtest::backtest::engine_core::EngineOptions;
use backtest::backtest::fixture::ReplayFixture;
use backtest::backtest::fixture::SyntheticDataOptions;
use backtest::backtest::identity::build_default_manifest;
use backtest::backtest::outcome::BacktestOutcome;
use backtest::backtest::preflight::preflight_data;
use backtest::backtest::subscriptions::runtime_subscriptions;
use harness_trading::program::StrategyInstance;

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
pub(super) fn run_native_engine_outcome(
    configuration: &BacktestConfig,
    engine_options: &EngineOptions,
    strategies: Vec<StrategyInstance>,
    fixture: ReplayFixture,
    synthetic_options: &SyntheticDataOptions,
) -> Result<BacktestOutcome, BacktestJobError> {
    // The null transform is a data boundary: it runs before any preflight or
    // manifest identity is derived so every downstream record describes the
    // transformed events, and its recomputed fingerprints match the replay.
    let fixture = backtest::backtest::null::apply_null_transform(configuration, &fixture)?;
    let preflight = preflight_data(configuration, &fixture)?;
    let manifest = build_default_manifest(configuration, &fixture, &preflight)?;
    let mut engine_options = engine_options.clone();
    engine_options.history = crate::history::resources().map_err(BacktestJobError::Io)?;
    Ok(run_native_engine(
        configuration,
        &fixture,
        &preflight,
        manifest,
        &engine_options,
        strategies,
        synthetic_options,
    )?)
}
