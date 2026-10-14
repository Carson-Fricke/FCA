//! Native Rust backtest runner for the harness HTTP launch route.
//!
//! [`NativeBacktestRunner`] plugs the Rust engine into the
//! [`BacktestRunner`] seam used by [`crate::http::launcher::InProcessLauncher`].
//! The submitted JSON configuration is decoded into the engine's typed
//! [`BacktestConfig`] through the same YAML value model the file loader uses,
//! then the engine drives the configured JIT-authored Rust programs through
//! the `harness-strategy-jit` seam — the same entry point the `backtest-job`
//! command uses.
//!
//! Market data streams from the market-lake HTTP routes through
//! `harness-lakeclient`; the endpoint resolves from `LAKE_API_URL` /
//! `LAKE_API_BASE_URL` and `LAKE_API__BEARER_TOKEN`. A run without a
//! configured lake fails loudly and never falls back to local files or a
//! synthetic fixture.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::EngineOptions;
use backtest::backtest::screen::ScreenConfig;
use harness_lakeclient::BlockingLakeClient;
use serde_json::Value;

use super::configuration::ValidatedConfiguration;
use super::launcher::{BacktestRunner, RunOutcome};
use super::screener::{ScreenRunOutcome, ScreeningRunner};

/// Native Rust backtest runner used by the durable API composition.
///
/// Every run streams the market-lake routes. The production composition leaves
/// [`NativeBacktestRunner::lake_client`] empty and resolves the client from the
/// process environment; tests and embedders may inject one explicitly.
#[derive(Debug, Clone, Default)]
pub struct NativeBacktestRunner {
    /// Optional directory the engine writes its own artifact set beneath.
    pub output_root: Option<PathBuf>,
    /// Optional explicit lake client; environment resolution applies when absent.
    pub lake_client: Option<Arc<BlockingLakeClient>>,
}
impl NativeBacktestRunner {
    /// Compose a runner writing engine artifacts beneath `output_root`.
    #[must_use]
    pub fn with_output_root(output_root: PathBuf) -> Self {
        Self {
            output_root: Some(output_root),
            lake_client: None,
        }
    }

    /// Compose a runner over an explicit market-lake client.
    #[must_use]
    pub fn with_lake_client(output_root: PathBuf, client: Arc<BlockingLakeClient>) -> Self {
        Self {
            output_root: Some(output_root),
            lake_client: Some(client),
        }
    }

    fn resolved_lake_client(&self) -> Result<Arc<BlockingLakeClient>, String> {
        match &self.lake_client {
            Some(client) => Ok(Arc::clone(client)),
            None => BlockingLakeClient::from_env()
                .map(Arc::new)
                .map_err(|error| error.to_string()),
        }
    }
}

impl BacktestRunner for NativeBacktestRunner {
    fn run(
        &self,
        configuration: &ValidatedConfiguration,
        progress: harness_contracts::progress::ProgressReporter,
    ) -> Result<RunOutcome, String> {
        let mut native = BacktestConfig::from_yaml(&configuration.document.to_string())
            .map_err(|error| error.to_string())?;
        let client = self
            .resolved_lake_client()?
            .reporting(progress)
            .map_err(|error| error.to_string())?;
        let directory = match &self.output_root {
            Some(root) => root.join(&native.job_id),
            None => std::env::temp_dir()
                .join("harness-api-native")
                .join(&native.job_id),
        };
        let (outcome, _artifact_path) =
            match configuration.document.get("shell").and_then(Value::as_str) {
                Some("study") => {
                    // A study replays bars only: its window clamps to the
                    // lake's complete bar coverage for the configured interval.
                    if !native.symbols.is_empty() {
                        crate::local::clamp_study_window(&client, &mut native)?;
                    }
                    harness_strategy_jit::run_study_configuration(
                        &native,
                        &EngineOptions::default(),
                        Some(directory.as_path()),
                        &client,
                    )
                    .map_err(|error| error.to_string())?
                }
                _ => harness_strategy_jit::run_backtest_configuration(
                    &native,
                    &EngineOptions::default(),
                    Some(directory.as_path()),
                    &client,
                )
                .map_err(|error| error.to_string())?,
            };
        let mut artifacts = BTreeMap::new();
        collect_artifacts(&directory, &directory, &mut artifacts);
        Ok(RunOutcome {
            completed: outcome.completed(),
            artifacts,
        })
    }
}

/// Native Rust screening runner used by the durable API composition.
///
/// Runs the JIT screening path with the real
/// [`LakeScreeningSource`](harness_strategy_jit::LakeScreeningSource) reading
/// the market-lake HTTP routes. A missing or rejected lake configuration fails
/// the launch loudly; no synthetic data is ever substituted. The runner
/// gathers every file the run wrote under its output directory so the launcher
/// can store them.
#[derive(Debug, Clone, Default)]
pub struct NativeScreenRunner {
    /// Optional directory the run writes its `screen.json` beneath.
    pub output_root: Option<PathBuf>,
    /// Optional managed JIT workspace root.
    pub workspace_root: Option<PathBuf>,
}

impl NativeScreenRunner {
    /// Compose a runner writing artifacts beneath `output_root`.
    #[must_use]
    pub fn with_output_root(output_root: PathBuf) -> Self {
        Self {
            output_root: Some(output_root),
            workspace_root: None,
        }
    }
}

impl ScreeningRunner for NativeScreenRunner {
    fn run(
        &self,
        configuration: &ScreenConfig,
        output_dir: Option<&Path>,
    ) -> Result<ScreenRunOutcome, String> {
        let directory = output_dir.map_or_else(
            || {
                self.output_root.as_ref().map_or_else(
                    || {
                        std::env::temp_dir()
                            .join("harness-api-native-screens")
                            .join(&configuration.job_id)
                    },
                    |root| root.join(&configuration.job_id),
                )
            },
            Path::to_path_buf,
        );
        let options = harness_strategy_jit::ScreenRunOptions {
            workspace_root: self.workspace_root.clone(),
        };
        let source = harness_strategy_jit::LakeScreeningSource::from_env()
            .map_err(|error| error.to_string())?;
        let (outcome, _artifact_path) = harness_strategy_jit::run_screening_configuration(
            configuration,
            &source,
            &options,
            Some(directory.as_path()),
        )
        .map_err(|error| error.to_string())?;
        let mut artifacts = BTreeMap::new();
        collect_artifacts(&directory, &directory, &mut artifacts);
        Ok(ScreenRunOutcome {
            completed: outcome.succeeded(),
            rows: outcome.row_count() as u64,
            artifacts,
            error: outcome.error().map(str::to_string),
        })
    }
}

fn collect_artifacts(base: &Path, directory: &Path, output: &mut BTreeMap<String, Vec<u8>>) {
    let Ok(entries) = std::fs::read_dir(directory) else {
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
        } else if let Ok(payload) = std::fs::read(&path) {
            if let Ok(relative) = path.strip_prefix(base) {
                output.insert(relative.to_string_lossy().replace('\\', "/"), payload);
            }
        }
    }
}
