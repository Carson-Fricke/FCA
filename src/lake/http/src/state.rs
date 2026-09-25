//! Injected state and request-boundary configuration for the lake API.

use std::sync::Arc;

use lake_catalog::{
    CatalogRepository, ContractRepository, DailyFrontSetRepository, JobRepository,
    ListingRepository,
};
use lake_contracts::DateTime;
use lake_coverage::EnsureJobLauncher;
use lake_query::{EnsureCallback, FileUrlIssuer};

/// The clock provider used to classify currently open sessions.
pub type ApiClock = Arc<dyn Fn() -> DateTime + Send + Sync>;

/// Injected repositories and seams the routes compose over.
///
/// The composition root owns every adapter; this struct only carries the
/// capabilities the published contract needs, so the HTTP boundary stays free
/// of configuration and storage dependencies.
pub struct LakeApiState {
    /// Coverage catalog the requests are classified against.
    pub catalog: Arc<dyn CatalogRepository>,
    /// Persisted ensure-job state.
    pub jobs: Arc<dyn JobRepository>,
    /// Optional point-in-time listings repository.
    pub listings: Option<Arc<dyn ListingRepository>>,
    /// Optional option contract metadata.
    pub contracts: Option<Arc<dyn ContractRepository>>,
    /// Optional date-specific option front-set membership.
    pub front_sets: Option<Arc<dyn DailyFrontSetRepository>>,
    /// Optional read-URL issuer for file and raw queries.
    pub sas_issuer: Option<Arc<dyn FileUrlIssuer>>,
    /// Optional ensure launcher; a provider-free no-op when absent.
    pub ensure_launcher: Option<EnsureJobLauncher>,
    /// Optional coverage-ensure callback used by the query route.
    pub ensure_callback: Option<EnsureCallback>,
    /// Maximum seconds one ensure request waits for its launcher.
    pub ensure_wait_deadline_seconds: f64,
    /// Static bearer token; `None` fails every protected request closed.
    pub bearer_token: Option<String>,
    /// Coverage clock provider; a test injects a fixed instant.
    pub now: ApiClock,
}

impl std::fmt::Debug for LakeApiState {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("LakeApiState")
            .field("listings", &self.listings.is_some())
            .field("contracts", &self.contracts.is_some())
            .field("front_sets", &self.front_sets.is_some())
            .field("sas_issuer", &self.sas_issuer.is_some())
            .field("ensure_launcher", &self.ensure_launcher.is_some())
            .field("ensure_callback", &self.ensure_callback.is_some())
            .field(
                "ensure_wait_deadline_seconds",
                &self.ensure_wait_deadline_seconds,
            )
            .field("bearer_token", &self.bearer_token.is_some())
            .finish_non_exhaustive()
    }
}

/// Request-boundary configuration resolved when the router is built.
#[derive(Debug, Clone, PartialEq)]
pub struct LakeApiConfig {
    /// Static bearer token; `None` fails every protected request closed.
    pub bearer_token: Option<String>,
    /// Maximum seconds one ensure request waits for its launcher.
    pub ensure_wait_deadline_seconds: f64,
}

impl Default for LakeApiConfig {
    fn default() -> Self {
        Self {
            bearer_token: None,
            ensure_wait_deadline_seconds: DEFAULT_ENSURE_WAIT_DEADLINE_SECONDS,
        }
    }
}

/// The Python deployment default from `config/market-lake/settings.yaml`.
pub(crate) const DEFAULT_ENSURE_WAIT_DEADLINE_SECONDS: f64 = 20.0;

/// The state passed to every handler and the auth middleware.
#[derive(Clone)]
pub(crate) struct ApiContext {
    /// Injected repositories and seams.
    pub state: Arc<LakeApiState>,
    /// Effective static token.
    pub bearer_token: Option<String>,
    /// Effective launcher wait deadline in seconds.
    pub ensure_wait_deadline_seconds: f64,
}

impl ApiContext {
    /// Resolve the effective context from the state and request configuration.
    ///
    /// The configuration wins when it carries a value; otherwise the state
    /// supplies it, and the deployment default applies when neither does.
    pub(crate) fn new(state: Arc<LakeApiState>, config: &LakeApiConfig) -> Self {
        let bearer_token = config
            .bearer_token
            .clone()
            .or_else(|| state.bearer_token.clone())
            .filter(|token| !token.trim().is_empty());
        let deadline = if is_valid_deadline(config.ensure_wait_deadline_seconds) {
            config.ensure_wait_deadline_seconds
        } else if is_valid_deadline(state.ensure_wait_deadline_seconds) {
            state.ensure_wait_deadline_seconds
        } else {
            DEFAULT_ENSURE_WAIT_DEADLINE_SECONDS
        };
        Self {
            state,
            bearer_token,
            ensure_wait_deadline_seconds: deadline,
        }
    }

    /// The coverage instant for this request.
    pub(crate) fn coverage_as_of(&self) -> DateTime {
        (self.state.now)()
    }
}

fn is_valid_deadline(seconds: f64) -> bool {
    seconds.is_finite() && seconds > 0.0
}
