//! Shared application state and composition for the harness HTTP boundary.

use std::path::PathBuf;
use std::sync::Arc;

use mcp::server::McpServer;

use super::clock::{Clock, SystemClock};
use super::configuration::{ConfigurationParser, StrictJsonConfigurationParser};
use super::launcher::{
    BacktestLauncher, BacktestRunner, InProcessLauncher, UnportedBacktestRunner,
};
use super::mcp::compose_local_server;
use super::native::NativeBacktestRunner;
use super::store::{
    FileSystemJobRepository, FileSystemRunStore, InMemoryJobRepository, InMemoryRunStore,
    JobRepository, RunStore, SasIssuer,
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
    /// Static bearer token, or `None` to fail closed.
    pub bearer_token: Option<String>,
    /// Results store.
    pub run_store: Arc<dyn RunStore>,
    /// Launch-job repository.
    pub job_repository: Arc<dyn JobRepository>,
    /// Backtest launcher.
    pub launcher: Arc<dyn BacktestLauncher>,
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
    /// Results store.
    pub run_store: Arc<dyn RunStore>,
    /// Launch-job repository.
    pub job_repository: Arc<dyn JobRepository>,
    /// Backtest launcher.
    pub launcher: Arc<dyn BacktestLauncher>,
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
    /// The default launcher runs the unported-engine stub, so a launch records
    /// a failed job until a real [`super::launcher::BacktestRunner`] is
    /// composed.
    #[must_use]
    pub fn in_memory() -> Self {
        let run_store = Arc::new(InMemoryRunStore::new());
        let job_repository = Arc::new(InMemoryJobRepository::new());
        let clock = Arc::new(SystemClock);
        let launcher = Arc::new(InProcessLauncher::new(
            run_store.clone(),
            job_repository.clone(),
            Arc::new(UnportedBacktestRunner),
            clock.clone(),
        ));
        Self {
            bearer_token: None,
            run_store,
            job_repository,
            launcher,
            sas_issuer: None,
            clock,
            configuration_parser: Arc::new(StrictJsonConfigurationParser),
            mcp_server: None,
        }
    }

    /// Build a durable app config rooted at `root` driving the native engine.
    ///
    /// Runs, jobs, and artifacts persist under `root` and remain queryable by a
    /// fresh process composed from the same root.
    #[must_use]
    pub fn durable(root: impl Into<PathBuf>) -> Self {
        Self::durable_with_runner(root, Arc::new(NativeBacktestRunner::default()))
    }

    /// Build a durable app config rooted at `root` with an injected runner.
    #[must_use]
    pub fn durable_with_runner(root: impl Into<PathBuf>, runner: Arc<dyn BacktestRunner>) -> Self {
        let root = root.into();
        let run_store: Arc<dyn RunStore> = Arc::new(FileSystemRunStore::new(&root));
        let job_repository: Arc<dyn JobRepository> = Arc::new(FileSystemJobRepository::new(&root));
        let clock = Arc::new(SystemClock);
        let launcher = Arc::new(InProcessLauncher::new(
            run_store.clone(),
            job_repository.clone(),
            runner,
            clock.clone(),
        ));
        Self {
            bearer_token: None,
            run_store,
            job_repository,
            launcher,
            sas_issuer: None,
            clock,
            configuration_parser: Arc::new(StrictJsonConfigurationParser),
            mcp_server: None,
        }
    }

    /// Build a durable app config rooted at the environment-resolved root.
    #[must_use]
    pub fn durable_default() -> Self {
        Self::durable(results_root_from_env())
    }

    pub(crate) fn into_state(self, bearer_token: Option<String>) -> AppState {
        let mcp_server = match self.mcp_server {
            Some(server) => Some(server),
            None => compose_local_server(self.run_store.clone(), self.job_repository.clone()).ok(),
        };
        AppState {
            bearer_token,
            run_store: self.run_store,
            job_repository: self.job_repository,
            launcher: self.launcher,
            sas_issuer: self.sas_issuer,
            clock: self.clock,
            configuration_parser: self.configuration_parser,
            mcp_server,
        }
    }
}
