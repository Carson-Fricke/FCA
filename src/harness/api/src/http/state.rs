//! Shared application state and composition for the harness HTTP boundary.

use std::sync::Arc;

use super::clock::{Clock, SystemClock};
use super::configuration::{ConfigurationParser, StrictJsonConfigurationParser};
use super::launcher::{BacktestLauncher, InProcessLauncher, UnportedBacktestRunner};
use super::store::{InMemoryJobRepository, InMemoryRunStore, JobRepository, RunStore, SasIssuer};

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
        }
    }

    pub(crate) fn into_state(self, bearer_token: Option<String>) -> AppState {
        AppState {
            bearer_token,
            run_store: self.run_store,
            job_repository: self.job_repository,
            launcher: self.launcher,
            sas_issuer: self.sas_issuer,
            clock: self.clock,
            configuration_parser: self.configuration_parser,
        }
    }
}
