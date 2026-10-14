//! Configuration-level run entry points over the JIT seam.
//!
//! These are the direct replacements for the removed embedded-Python
//! runners: they build the configuration's authored instances in the default
//! managed workspace and hand them to the native engine job seam.

use std::path::{Path, PathBuf};

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::{BacktestOutcome, EngineOptions, MarketSubscription};
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

/// Construct programs and return canonical effective inputs before fetching data.
///
/// # Errors
/// Returns a configuration error for invalid source, parameters, or declarations.
pub fn configuration_subscriptions(
    configuration: &BacktestConfig,
) -> Result<Vec<MarketSubscription>, ConfigurationRunError> {
    let workspace = ManagedWorkspace::open_default()?;
    let programs = build_instances(configuration, &workspace)?;
    let mut inputs: Vec<MarketSubscription> = Vec::new();
    for program in &programs {
        inputs.extend(
            program.runtime.subscriptions().ok_or_else(|| {
                JitError::Invalid("program must declare its subscriptions".into())
            })?,
        );
    }
    let inputs = inputs
        .into_iter()
        .map(|input| match input.data_type.as_str() {
            "bars" => Ok(harness_strategy_sdk::Subscription::Bars {
                symbols: input.symbols,
                interval: input
                    .interval
                    .ok_or_else(|| JitError::Invalid("bars require an interval".into()))?,
            }),
            "quote" => Ok(harness_strategy_sdk::Subscription::Quote {
                symbols: input.symbols,
            }),
            "trade" => Ok(harness_strategy_sdk::Subscription::Trade {
                symbols: input.symbols,
            }),
            _ => Err(JitError::Invalid(
                "unsupported subscription data type".into(),
            )),
        })
        .collect::<Result<Vec<_>, JitError>>()?;
    let canonical =
        harness_strategy_sdk::canonical_subscriptions(inputs).map_err(JitError::Invalid)?;
    Ok(canonical
        .into_iter()
        .map(crate::runtime::engine_subscription)
        .collect())
}

/// Inspect compiled, loaded, and constructed program inputs without fetching market data.
///
/// # Errors
/// Returns structured authored diagnostics or a non-cacheable configuration/runtime failure.
pub fn inspect_configuration(
    configuration: &BacktestConfig,
) -> Result<serde_json::Value, crate::compile::ValidationFailure> {
    use crate::compile::ValidationFailure;
    use backtest::backtest::engine_core::normalize_subscriptions;

    let subscriptions =
        configuration_subscriptions(configuration).map_err(|error| match error {
            ConfigurationRunError::Jit(error) => ValidationFailure::for_program(
                error,
                crate::config::program_kind(&configuration.shell),
            ),
            ConfigurationRunError::Job(error) => ValidationFailure::new(error.to_string()),
        })?;
    let subscriptions = normalize_subscriptions(&subscriptions)
        .map_err(|error| ValidationFailure::configuration(error.to_string()))?;
    let expected = normalize_subscriptions(&configuration.data_subscriptions)
        .map_err(|error| ValidationFailure::configuration(error.to_string()))?;
    if expected != subscriptions {
        return Err(ValidationFailure::configuration(
            "data.subscriptions does not match runtime subscription union",
        ));
    }
    Ok(serde_json::json!({
        "valid": true,
        "kind": if configuration.shell == "study" { "study" } else { "strategy" },
        "shell": configuration.shell,
        "instances": configuration.strategy_instances.len(),
        "subscriptions": subscriptions,
    }))
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
    client
        .report_progress(harness_contracts::progress::ExecutionProgress::stage(
            harness_contracts::progress::ExecutionStage::Compiling,
        ))
        .map_err(|error| {
            ConfigurationRunError::Job(backtest::backtest::job::BacktestJobError::Io(
                error.to_string(),
            ))
        })?;
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
    client
        .report_progress(harness_contracts::progress::ExecutionProgress::stage(
            harness_contracts::progress::ExecutionStage::Compiling,
        ))
        .map_err(|error| {
            ConfigurationRunError::Job(backtest::backtest::job::BacktestJobError::Io(
                error.to_string(),
            ))
        })?;
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
