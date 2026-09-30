//! Configuration-level run entry points over the JIT seam.
//!
//! These are the direct replacements for the removed embedded-Python
//! runners: they build the configuration's authored instances in the default
//! managed workspace and hand them to the native engine job seam.

use std::path::{Path, PathBuf};

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::{BacktestOutcome, EngineOptions};
use backtest::backtest::job::BacktestJobError;
use backtest::backtest::job::{
    run_native_job_with_lake_client, run_native_study_job_with_lake_client,
};
use harness_lakeclient::BlockingLakeClient;

use crate::config::build_instances;
use crate::error::JitError;
use crate::workspace::ManagedWorkspace;

/// One configuration run failure: authored-program work or the engine job.
#[derive(Debug)]
pub enum ConfigurationRunError {
    /// The authored programs failed to build or load.
    Jit(JitError),
    /// The engine job failed.
    Job(BacktestJobError),
}

impl std::fmt::Display for ConfigurationRunError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Jit(error) => write!(formatter, "{error}"),
            Self::Job(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for ConfigurationRunError {}

impl From<JitError> for ConfigurationRunError {
    fn from(error: JitError) -> Self {
        Self::Jit(error)
    }
}

impl From<BacktestJobError> for ConfigurationRunError {
    fn from(error: BacktestJobError) -> Self {
        Self::Job(error)
    }
}

/// Validate one configuration's authored programs without replaying history.
///
/// Every instance is compiled, loaded, and constructed over the default
/// managed workspace, exactly as a run would, so a source that fails to build
/// or omits the SDK handshake exports is reported before any data is fetched.
/// Returns the number of validated instances.
///
/// # Errors
///
/// Returns [`JitError`] under the same conditions as
/// [`build_instances`](crate::config::build_instances).
pub fn validate_configuration(configuration: &BacktestConfig) -> Result<usize, JitError> {
    let workspace = ManagedWorkspace::open_default()?;
    let programs = build_instances(configuration, &workspace)?;
    Ok(programs.len())
}

/// Run one already-validated study configuration through JIT-authored study
/// programs and the native engine, writing the canonical `study.json`
/// artifact set.
///
/// The run window's market data is ensured and streamed through `client`; the
/// lake can never be bypassed with a local path.
///
/// # Errors
///
/// Returns [`ConfigurationRunError`] when an authored program cannot be
/// built, the lake cannot serve the window, or the engine rejects the run.
pub fn run_study_configuration(
    configuration: &BacktestConfig,
    engine_options: &EngineOptions,
    output_dir: Option<&Path>,
    client: &BlockingLakeClient,
) -> Result<(BacktestOutcome, PathBuf), ConfigurationRunError> {
    let workspace = ManagedWorkspace::open_default()?;
    let programs = build_instances(configuration, &workspace)?;
    Ok(run_native_study_job_with_lake_client(
        configuration,
        engine_options,
        output_dir,
        programs,
        client,
    )?)
}

/// Run one already-validated backtest configuration through JIT-authored
/// strategy programs and the native engine, writing the canonical
/// `backtest.json` artifact set.
///
/// # Errors
///
/// Returns [`ConfigurationRunError`] under the same conditions as
/// [`run_study_configuration`].
pub fn run_backtest_configuration(
    configuration: &BacktestConfig,
    engine_options: &EngineOptions,
    output_dir: Option<&Path>,
    client: &BlockingLakeClient,
) -> Result<(BacktestOutcome, PathBuf), ConfigurationRunError> {
    let workspace = ManagedWorkspace::open_default()?;
    let strategies = build_instances(configuration, &workspace)?;
    Ok(run_native_job_with_lake_client(
        configuration,
        engine_options,
        output_dir,
        strategies,
        client,
    )?)
}
