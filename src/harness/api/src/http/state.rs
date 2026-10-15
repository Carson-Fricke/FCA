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
use super::native::{NativeBacktestRunner, NativeScreenRunner};
use super::screener::{InProcessScreenLauncher, ScreenLauncher};
use super::store::{FileSystemJobStore, InMemoryJobStore, JobStore, SasIssuer};

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
    /// Authoritative job store.
    pub job_store: Arc<dyn JobStore>,
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
    pub job_store: Arc<dyn JobStore>,
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
    /// The default launcher runs the unported-engine stub, so a launch records
    /// a failed job until a real [`super::launcher::BacktestRunner`] is
    /// composed.
    #[must_use]
    pub fn in_memory() -> Self {
        let job_store = Arc::new(InMemoryJobStore::new());
        let clock = Arc::new(SystemClock);
        let launcher = Arc::new(InProcessLauncher::new(
            job_store.clone(),
            Arc::new(UnportedBacktestRunner),
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

    /// Build a durable app config rooted at `root` driving the native engine.
    ///
    /// Jobs and artifacts persist under `root` and remain queryable by a
    /// fresh process composed from the same root.
    #[must_use]
    pub fn durable(root: impl Into<PathBuf>) -> Self {
        Self::durable_with_runner(root, Arc::new(NativeBacktestRunner::default()))
    }

    /// Build a durable app config rooted at `root` with an injected runner.
    #[must_use]
    pub fn durable_with_runner(root: impl Into<PathBuf>, runner: Arc<dyn BacktestRunner>) -> Self {
        let root = root.into();
        let job_store: Arc<dyn JobStore> = Arc::new(FileSystemJobStore::new(&root));
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

    /// Build a durable app config rooted at the environment-resolved root.
    #[must_use]
    pub fn durable_default() -> Self {
        Self::durable(results_root_from_env())
    }

    pub fn with_market_access(
        mut self,
        access: Arc<dyn harness_lakeclient::MarketAccess>,
    ) -> Result<Self, String> {
        let client = Arc::new(
            harness_lakeclient::BlockingLakeClient::from_access(access.clone())
                .map_err(|e| e.to_string())?,
        );
        self.launcher = Arc::new(InProcessLauncher::new(
            self.job_store.clone(),
            Arc::new(NativeBacktestRunner {
                lake_client: Some(client.clone()),
                ..Default::default()
            }),
            self.clock.clone(),
        ));
        self.screener_launcher = Arc::new(InProcessScreenLauncher::new(
            self.job_store.clone(),
            Arc::new(NativeScreenRunner {
                lake_client: Some(client),
                ..Default::default()
            }),
            self.clock.clone(),
        ));
        self.mcp_server = Some(
            super::mcp::compose_server(self.job_store.clone(), Some(access))
                .map_err(|e| e.to_string())?,
        );
        Ok(self)
    }
    pub(crate) fn into_state(self, bearer_token: Option<String>) -> AppState {
        let mcp_server = match self.mcp_server {
            Some(server) => Some(server),
            None => compose_local_server(self.job_store.clone()).ok(),
        };
        AppState {
            bearer_token,
            job_store: self.job_store,
            launcher: self.launcher,
            screener_launcher: self.screener_launcher,
            sas_issuer: self.sas_issuer,
            clock: self.clock,
            configuration_parser: self.configuration_parser,
            mcp_server,
        }
    }
}
