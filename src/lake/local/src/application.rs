//! Local-only composition for the market-lake HTTP application.
//!
//! Ports `lake/local/application.py`: builds the published API over local files
//! and catalog state without Azure clients, installing the on-demand ensure
//! launcher and a query ensure callback that runs the same backfill.

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::Router;
use lake_catalog::{CatalogRepository, JobRepository, JsonCatalogStore, MemoryJobRepository};
use lake_configuration::{LakeSettings, UniverseSettings};
use lake_contracts::DateTime;
use lake_coverage::{ensure_job_id, EnsureJob, EnsureJobLauncher, EnsureSpec};
use lake_http::{create_app, LakeApiConfig, LakeApiState};
use lake_query::{
    BoundedDuckDbQuery, EnsureCallback, FileUrlIssuer, QueryApplicationError, QueryLimits,
};
use lake_storage::{LocalFilesystemStore, ReadableBlobStore};

use crate::ensure::{local_ensure_deadline_seconds, LocalEnsureLauncher};
use crate::ingest::LocalIngestError;
use crate::state::{lookup, LocalFileIssuer, LocalLayout};

const BEARER_TOKEN_ENV: &str = "LAKE_API__BEARER_TOKEN";

/// Provider seams for tests, mirroring the Python app's injectable overrides.
#[derive(Default)]
pub struct LocalProviders {
    /// Injected equity provider; built from credentials when absent.
    pub equity: Option<Arc<dyn lake_ingestion::EquityDataProvider>>,
    /// Injected crypto provider; built from credentials when absent.
    pub crypto: Option<Arc<dyn lake_ingestion::CryptoDataProvider>>,
    /// Injected bars provider; built from credentials when absent.
    pub bars: Option<Arc<dyn lake_ingestion::BarsDataProvider>>,
    /// Option dataset port; production uses the credentialed Alpaca client.
    pub options: Option<Arc<dyn lake_ingestion::options::transaction::OptionMarketDataClient>>,
    /// Point-in-time contract master and historical quote input port.
    pub option_selection:
        Option<Arc<dyn lake_ingestion::options::resolve::OptionSelectionProvider>>,
}

/// Build the API over local files and catalog state without Azure clients.
///
/// A real on-demand ensure launcher is installed so a request for symbols the
/// local lake has not fetched yet triggers a provider backfill instead of a
/// no-op that could never satisfy coverage. Jobs live in memory: local mode
/// does not persist job state.
pub fn create_local_app(
    layout: &LocalLayout,
    environ: &BTreeMap<String, String>,
    now: Arc<dyn Fn() -> DateTime + Send + Sync>,
) -> Result<Router, LocalIngestError> {
    create_local_app_with(layout, environ, now, LocalProviders::default())
}

/// [`create_local_app`] with injected provider seams for tests.
pub fn create_local_app_with(
    layout: &LocalLayout,
    environ: &BTreeMap<String, String>,
    now: Arc<dyn Fn() -> DateTime + Send + Sync>,
    providers: LocalProviders,
) -> Result<Router, LocalIngestError> {
    let settings = LakeSettings::load(None, environ)?;
    let universes = UniverseSettings::load(None, environ)?;
    let bearer_token = lookup(environ, BEARER_TOKEN_ENV)
        .filter(|token| !token.trim().is_empty())
        .or_else(|| {
            settings
                .api
                .bearer_token
                .clone()
                .filter(|token| !token.trim().is_empty())
        })
        .ok_or_else(|| LocalIngestError::new("LAKE_API__BEARER_TOKEN is required"))?;
    let deadline = local_ensure_deadline_seconds(environ)?;
    layout.initialize()?;

    let catalog: Arc<JsonCatalogStore> = Arc::new(JsonCatalogStore::open(layout.catalog_path())?);
    let repository: Arc<dyn CatalogRepository> = catalog.clone();
    let contracts: Arc<dyn lake_catalog::ContractRepository> =
        Arc::new(crate::options::LocalOptionContracts::open(layout)?);
    let client = Arc::new(
        lake_providers::AlpacaClient::with_reqwest(
            crate::ingest::provider_config(&settings),
            crate::ingest::shared_governor(&settings),
        )
        .map_err(|e| LocalIngestError::new(e.to_string()))?,
    );
    let credentials_present = settings
        .provider
        .api_key
        .as_ref()
        .is_some_and(|s| !s.trim().is_empty())
        && settings
            .provider
            .api_secret
            .as_ref()
            .is_some_and(|s| !s.trim().is_empty());
    let selection_available = providers.option_selection.is_some() || credentials_present;
    let options = providers.options.or_else(|| {
        credentials_present.then(|| {
            client.clone() as Arc<dyn lake_ingestion::options::transaction::OptionMarketDataClient>
        })
    });
    let option_selection = providers.option_selection.unwrap_or_else(|| client.clone());
    let resolver = Arc::new(
        lake_ingestion::options::resolve::OptionContractResolver::new(
            option_selection,
            contracts.clone(),
        ),
    );
    let option_resolver: lake_http::state::OptionResolver = Arc::new(move |selector| {
        let resolver = resolver.clone();
        Box::pin(async move {
            if !selection_available {
                return Err(lake_contracts::DataError::value(
                    "Alpaca credentials are required for option resolution",
                ));
            }
            resolver.resolve(&selector).await
        })
    });
    let jobs: Arc<dyn JobRepository> = Arc::new(MemoryJobRepository::new());
    let issuer: Arc<dyn FileUrlIssuer> = Arc::new(LocalFileIssuer::new(layout.objects()));
    let sql_limits = QueryLimits::new(
        settings.api.sql.max_scan_bytes,
        settings.api.sql.max_result_bytes,
    )
    .map_err(LocalIngestError::new)?;
    let sql_force_authorized = settings.api.sql.authorized_force_override_default;
    let bounded_query: Arc<BoundedDuckDbQuery> =
        Arc::new(BoundedDuckDbQuery::new(layout.objects(), sql_limits));
    let read_store: Arc<dyn ReadableBlobStore> =
        Arc::new(LocalFilesystemStore::new(layout.objects()));
    let launcher = Arc::new(
        LocalEnsureLauncher::new(layout.clone(), environ.clone(), settings, universes, now())?
            .with_catalog(Arc::clone(&catalog))
            .with_jobs(Arc::clone(&jobs))
            .with_providers(providers.equity, providers.crypto)
            .with_options(options, contracts.clone())
            .with_bars_provider(providers.bars),
    );
    let ensure_launcher: EnsureJobLauncher = launcher.launcher();
    let callback_launcher = Arc::clone(&ensure_launcher);
    let ensure_callback: EnsureCallback = Arc::new(
        move |asset_class, data_type, interval, symbol, start_date, end_date| {
            let launcher = Arc::clone(&callback_launcher);
            Box::pin(async move {
                let spec = EnsureSpec {
                    asset_class,
                    data_type,
                    symbol,
                    start_date,
                    end_date,
                    interval,
                };
                let job = EnsureJob {
                    job_id: ensure_job_id(&spec).unwrap_or_default(),
                    request: spec,
                    gaps: Vec::new(),
                };
                launcher(job).await.map_err(|error| {
                    QueryApplicationError::new(
                        "ensure_failed",
                        "coverage ensure failed",
                        Some(serde_json::json!({ "error": error.to_string() })),
                    )
                })
            })
        },
    );

    let state = Arc::new(LakeApiState {
        catalog: repository,
        jobs,
        listings: None,
        contracts: Some(contracts),
        front_sets: None,
        option_resolver: Some(option_resolver),
        sas_issuer: Some(issuer),
        read_store: Some(read_store),
        ensure_launcher: Some(ensure_launcher),
        ensure_callback: Some(ensure_callback),
        bounded_query: Some(bounded_query),
        authorized_force_override: sql_force_authorized,
        entra: None,
        ensure_wait_deadline_seconds: deadline,
        bearer_token: Some(bearer_token),
        now,
    });
    Ok(create_app(
        state,
        LakeApiConfig {
            bearer_token: None,
            ensure_wait_deadline_seconds: deadline,
        },
    ))
}
