//! Injected state and request-boundary configuration for the lake API.

use std::sync::Arc;

use crate::auth;
use crate::error::ApiError;
use lake_catalog::{
    CatalogRepository, ContractRepository, DailyFrontSetRepository, JobRepository,
    ListingRepository,
};
use lake_contracts::DateTime;
use lake_coverage::EnsureAdmission;
use lake_query::{BoundedDuckDbQuery, EnsureCallback, FileUrlIssuer};
use lake_storage::ReadableBlobStore;

/// The clock provider used to classify currently open sessions.
pub type ApiClock = Arc<dyn Fn() -> DateTime + Send + Sync>;
pub type OptionResolver = Arc<
    dyn Fn(
            lake_contracts::OptionSelector,
            Option<String>,
        ) -> futures_util::future::BoxFuture<
            'static,
            Result<lake_contracts::ResolvedOption, lake_contracts::DataError>,
        > + Send
        + Sync,
>;

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
    /// Resolve a strike or delta selector at its explicit historical timestamp.
    pub option_resolver: Option<OptionResolver>,
    /// Optional read-URL issuer for file and raw queries.
    pub sas_issuer: Option<Arc<dyn FileUrlIssuer>>,
    /// Optional blob-read seam backing the streaming read and reference routes.
    pub read_store: Option<Arc<dyn ReadableBlobStore>>,
    /// Durable ingestion admission; unavailable acquisition fails when absent.
    pub ensure_admission: Option<EnsureAdmission>,
    /// Optional coverage-ensure callback used by the query route.
    pub ensure_callback: Option<EnsureCallback>,
    /// Optional bounded DuckDB engine backing SQL-mode queries.
    pub bounded_query: Option<Arc<BoundedDuckDbQuery>>,
    /// Whether `force=true` is authorized for SQL queries.
    pub authorized_force_override: bool,
    /// Static bearer token; `None` fails every protected request closed.
    pub bearer_token: Option<String>,
    /// Optional Entra JWT validator; a JWT-shaped credential is validated
    /// against it before the static-token fallback applies.
    pub entra: Option<Arc<auth::EntraJwtAuthenticator>>,
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
            .field("read_store", &self.read_store.is_some())
            .field("ensure_admission", &self.ensure_admission.is_some())
            .field("ensure_callback", &self.ensure_callback.is_some())
            .field("bounded_query", &self.bounded_query.is_some())
            .field("authorized_force_override", &self.authorized_force_override)
            .field("bearer_token", &self.bearer_token.is_some())
            .field("entra", &self.entra.is_some())
            .finish_non_exhaustive()
    }
}

/// Request-boundary configuration resolved when the router is built.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct LakeApiConfig {
    /// Static bearer token; `None` fails every protected request closed.
    pub bearer_token: Option<String>,
}

/// The state passed to every handler and the auth middleware.
#[derive(Clone)]
pub(crate) struct ApiContext {
    /// Injected repositories and seams.
    pub state: Arc<LakeApiState>,
    /// Effective static token.
    pub bearer_token: Option<String>,
    /// Optional Entra JWT validator.
    pub entra: Option<Arc<auth::EntraJwtAuthenticator>>,
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
        let entra = state.entra.clone();
        Self {
            state,
            bearer_token,
            entra,
        }
    }

    /// The coverage instant for this request.
    pub(crate) fn coverage_as_of(&self) -> DateTime {
        (self.state.now)()
    }

    /// The injected blob-read seam, or the read-unavailable rejection.
    pub(crate) fn read_store(&self) -> Result<Arc<dyn ReadableBlobStore>, ApiError> {
        self.state.read_store.clone().ok_or_else(|| {
            ApiError::new(
                axum::http::StatusCode::SERVICE_UNAVAILABLE,
                "read_unavailable",
                "lake read storage is not configured",
            )
        })
    }
}
