//! Shared application state and composition for the harness HTTP boundary.

use std::path::PathBuf;
use std::sync::Arc;

use mcp::server::McpServer;

use super::clock::Clock;
use super::clock::SystemClock;
use super::configuration::ConfigurationParser;
use super::configuration::StrictJsonConfigurationParser;
use super::launcher::BacktestLauncher;
use super::launcher::BacktestRunner;
use super::launcher::InProcessLauncher;
use super::native::NativeBacktestRunner;
use super::native::NativeScreenRunner;
use super::screener::InProcessScreenLauncher;
use super::screener::ScreenLauncher;
use harness_execution::jobs::SasIssuer;
use harness_execution::jobs::{
    ArtifactReader, ExecutionRepository, JobAdmission, JobQueries, JobRetention,
};

/// Environment variable overriding the durable results root.
pub const RESULTS_ROOT_VARIABLE: &str = "HARNESS_RESULTS_ROOT";

/// Default durable results root under the working directory.
pub const DEFAULT_RESULTS_ROOT: &str = ".local/harness-results";

/// Resolve the durable results root from the environment, or the default.
#[must_use]
pub fn results_root_from_env() -> PathBuf {
    std::env::var(RESULTS_ROOT_VARIABLE)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .map_or_else(|| PathBuf::from(DEFAULT_RESULTS_ROOT), PathBuf::from)
}

/// Resolved, immutable state shared by every request handler.
#[derive(Clone)]
pub struct AppState {
    pub control: Arc<tokio::sync::Semaphore>,
    pub transfers: Arc<tokio::sync::Semaphore>,
    /// Static bearer token, or `None` to fail closed.
    pub bearer_token: Option<String>,
    /// Authoritative job store.
    pub admission: Arc<dyn JobAdmission>,
    pub queries: Arc<dyn JobQueries>,
    pub artifacts: Arc<dyn ArtifactReader>,
    pub retention: Arc<dyn JobRetention>,
    pub execution: Arc<dyn harness_execution::jobs::JobExecution>,
    /// Backtest launcher.
    pub launcher: Arc<dyn BacktestLauncher>,
    /// Screening launcher.
    pub screener_launcher: Arc<dyn ScreenLauncher>,
    /// Optional artifact SAS issuer.
    pub sas_issuer: Option<Arc<dyn SasIssuer>>,
    /// Clock used for job timestamps.
    pub clock: Arc<dyn Clock>,
    /// Submitted-configuration parser.
    pub configuration_parser: Arc<dyn ConfigurationParser>,
    /// Remote MCP server exposed at `POST /mcp`, when composed.
    pub mcp_server: Option<Arc<McpServer>>,
}

/// Composition inputs for [`super::create_app`].
pub struct AppConfig {
    /// Explicit bearer token; when `None` the runtime environment is read.
    pub bearer_token: Option<String>,
    /// Authoritative job store.
    pub job_store: Arc<dyn ExecutionRepository>,
    /// Backtest launcher.
    pub launcher: Arc<dyn BacktestLauncher>,
    /// Screening launcher.
    pub screener_launcher: Arc<dyn ScreenLauncher>,
    /// Optional artifact SAS issuer.
    pub sas_issuer: Option<Arc<dyn SasIssuer>>,
    /// Clock used for job timestamps.
    pub clock: Arc<dyn Clock>,
    /// Submitted-configuration parser.
    pub configuration_parser: Arc<dyn ConfigurationParser>,
    /// Remote MCP server exposed at `POST /mcp`, when composed.
    pub mcp_server: Option<Arc<McpServer>>,
}

impl AppConfig {
    /// Build an all-in-memory app config with the default parser and clock.
    ///
    /// Explicit in-process composition uses the current native runner.
    #[must_use]
    pub fn in_memory() -> Self {
        let job_store = Arc::new(harness_execution_storage::memory_store());
        let clock = Arc::new(SystemClock);
        let launcher = Arc::new(InProcessLauncher::new(
            job_store.clone(),
            Arc::new(NativeBacktestRunner::default()),
            clock.clone(),
        ));
        let screener_launcher = Arc::new(InProcessScreenLauncher::new(
            job_store.clone(),
            Arc::new(NativeScreenRunner::default()),
            clock.clone(),
        ));
        Self {
            bearer_token: None,
            job_store,
            launcher,
            screener_launcher,
            sas_issuer: None,
            clock,
            configuration_parser: Arc::new(StrictJsonConfigurationParser),
            mcp_server: None,
        }
    }

    /// Build a durable app config rooted at `root` with an injected runner.
    #[must_use]
    pub fn durable_with_runner(root: impl Into<PathBuf>, runner: Arc<dyn BacktestRunner>) -> Self {
        let root = root.into();
        let job_store: Arc<dyn ExecutionRepository> =
            Arc::new(harness_execution_storage::filesystem_store(&root));
        let clock = Arc::new(SystemClock);
        let launcher = Arc::new(InProcessLauncher::new(
            job_store.clone(),
            runner,
            clock.clone(),
        ));
        let screener_launcher = Arc::new(InProcessScreenLauncher::new(
            job_store.clone(),
            Arc::new(NativeScreenRunner::default()),
            clock.clone(),
        ));
        Self {
            bearer_token: None,
            job_store,
            launcher,
            screener_launcher,
            sas_issuer: None,
            clock,
            configuration_parser: Arc::new(StrictJsonConfigurationParser),
            mcp_server: None,
        }
    }

    pub(crate) fn into_state(self, bearer_token: Option<String>) -> AppState {
        AppState {
            bearer_token,
            admission: self.job_store.clone(),
            queries: self.job_store.clone(),
            artifacts: self.job_store.clone(),
            retention: self.job_store.clone(),
            execution: self.job_store,
            launcher: self.launcher,
            screener_launcher: self.screener_launcher,
            sas_issuer: self.sas_issuer,
            clock: self.clock,
            configuration_parser: self.configuration_parser,
            mcp_server: self.mcp_server,
            control: Arc::new(tokio::sync::Semaphore::new(8)),
            transfers: Arc::new(tokio::sync::Semaphore::new(4)),
        }
    }
}
