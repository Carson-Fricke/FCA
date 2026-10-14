//! Hosted Azure composition for the published market-lake API.
//!
//! Ports `lake.hosted.runtime`: builds the Azure Table catalog, durable job
//! repository, identity-based SAS issuer, and the lease-protected equity
//! ingest from non-secret environment configuration so the published API
//! serves real coverage and performs real ensure work. Missing configuration
//! selects the framework defaults instead of attempting Azure access.

use std::collections::BTreeMap;
use std::sync::{Arc, OnceLock};

use lake_catalog::{
    AzureTableCatalogRepository, AzureTableJobRepository, CatalogRepository, JobProgress,
    JobRepository,
};
use lake_configuration::{ConfigError, LakeSettings};
use lake_contracts::{DataError, DateTime};
use lake_coverage::{requested_coverage, EnsureJob, EnsureJobLauncher, EnsureSpec};
use lake_ingestion::{
    AzureAtomicIngestCommitter, CatalogTradeReader, EnsureIngest, EnsureProgress, EnsureProviders,
    LeasedAtomicIngestCommitter,
};
use lake_providers::{AlpacaClient, ProviderConfig, RequestGovernor};
use lake_query::{EnsureCallback, FileUrlIssuer, QueryApplicationError};
use lake_storage::{
    sas_issuer_from_account_url, AzureBlobSasIssuer, AzureReadableBlobStore, LeaseProvider,
    ReadableBlobStore, SasIssuer,
};

/// The default storage container name.
pub const DEFAULT_STORAGE_CONTAINER: &str = "lake";
/// The default coverage table name.
pub const DEFAULT_COVERAGE_TABLE: &str = "coverage";
/// The default jobs table name.
pub const DEFAULT_JOBS_TABLE: &str = "jobs";

const STORAGE_ACCOUNT_ENV: &str = "LAKE_STORAGE_ACCOUNT_NAME";
const STORAGE_CONTAINER_ENV: &str = "LAKE_STORAGE_CONTAINER";
const COVERAGE_TABLE_ENV: &str = "LAKE_COVERAGE_TABLE";
const JOBS_TABLE_ENV: &str = "LAKE_JOBS_TABLE";

/// A hosted ensure request the runtime cannot satisfy.
#[derive(Debug, Clone)]
pub struct HostedEnsureError(pub String);

impl HostedEnsureError {
    /// Wrap one failure detail.
    pub fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl std::fmt::Display for HostedEnsureError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl From<ConfigError> for HostedEnsureError {
    fn from(error: ConfigError) -> Self {
        Self(error.to_string())
    }
}

impl From<azure_core::Error> for HostedEnsureError {
    fn from(error: azure_core::Error) -> Self {
        Self(error.to_string())
    }
}

impl From<DataError> for HostedEnsureError {
    fn from(error: DataError) -> Self {
        Self(error.to_string())
    }
}

/// Azure-backed catalog, job, query, and ingest boundaries for hosting.
pub struct HostedRuntime {
    /// Loaded lake settings.
    pub settings: LakeSettings,
    catalog: Arc<AzureTableCatalogRepository>,
    jobs: Arc<dyn JobRepository>,
    contracts: Arc<dyn lake_catalog::ContractRepository>,
    sas_issuer: Arc<AzureBlobSasIssuer>,
    read_store: Arc<AzureReadableBlobStore>,
    blob_url: String,
    container_name: String,
    credential: Arc<dyn azure_core::auth::TokenCredential>,
    clock: Arc<dyn Fn() -> DateTime + Send + Sync>,
}

impl std::fmt::Debug for HostedRuntime {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("HostedRuntime")
            .field("blob_url", &self.blob_url)
            .field("container_name", &self.container_name)
            .finish()
    }
}

/// Read one non-empty, trimmed environment value.
fn environment_value(environ: &BTreeMap<String, String>, name: &str) -> Option<String> {
    let value = environ.get(name).map(String::as_str).unwrap_or_default();
    let trimmed = value.trim();
    (!trimmed.is_empty()).then(|| trimmed.to_string())
}

/// The current system-UTC instant at microsecond resolution.
fn system_now() -> DateTime {
    let micros = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_micros().min(i64::MAX as u128) as i64)
        .unwrap_or(0);
    DateTime::from_micros(micros)
}

impl HostedRuntime {
    /// Build the runtime from non-secret environment configuration.
    pub fn from_environment(
        environ: &BTreeMap<String, String>,
        clock: Option<Arc<dyn Fn() -> DateTime + Send + Sync>>,
    ) -> Result<Self, HostedEnsureError> {
        let Some(account) = environment_value(environ, STORAGE_ACCOUNT_ENV) else {
            return Err(HostedEnsureError::new(
                "LAKE_STORAGE_ACCOUNT_NAME is required for the hosted runtime",
            ));
        };
        let settings = LakeSettings::load(None, environ)?;
        // ACA startup selects the configured client_id in IDENTITY_ENDPOINT.
        // The pinned 0.21 App Service credential otherwise requests only the
        // system-assigned identity; AZURE_CLIENT_ID alone is insufficient.
        let credential: Arc<dyn azure_core::auth::TokenCredential> =
            Arc::new(azure_identity::DefaultAzureCredentialBuilder::new().build()?);
        let blob_url = format!("https://{account}.blob.core.windows.net");
        let table_url = format!("https://{account}.table.core.windows.net");
        let container_name = environment_value(environ, STORAGE_CONTAINER_ENV)
            .unwrap_or_else(|| DEFAULT_STORAGE_CONTAINER.to_string());
        let coverage_table = environment_value(environ, COVERAGE_TABLE_ENV)
            .unwrap_or_else(|| DEFAULT_COVERAGE_TABLE.to_string());
        let jobs_table = environment_value(environ, JOBS_TABLE_ENV)
            .unwrap_or_else(|| DEFAULT_JOBS_TABLE.to_string());
        let catalog = AzureTableCatalogRepository::new(Arc::new(
            lake_catalog::SdkTableEntityStore::from_account_url(
                &table_url,
                &coverage_table,
                Arc::clone(&credential),
            )?,
        ));
        let contracts = Arc::new(lake_catalog::AzureTableContractRepository::new(Arc::new(
            lake_catalog::SdkTableEntityStore::from_account_url(
                &table_url,
                &coverage_table,
                Arc::clone(&credential),
            )?,
        )));
        let jobs = AzureTableJobRepository::new(Arc::new(
            lake_catalog::SdkTableEntityStore::from_account_url(
                &table_url,
                &jobs_table,
                Arc::clone(&credential),
            )?,
        ));
        let sas_issuer = Arc::new(sas_issuer_from_account_url(
            &blob_url,
            &container_name,
            Arc::clone(&credential),
            i64::try_from(settings.api.sas.ttl_seconds).unwrap_or(900),
        )?);
        let read_store = Arc::new(AzureReadableBlobStore::from_account_url(
            &blob_url,
            &container_name,
            Arc::clone(&credential),
        )?);
        Ok(Self {
            settings,
            catalog: Arc::new(catalog),
            jobs: Arc::new(jobs),
            contracts,
            sas_issuer,
            read_store,
            blob_url,
            container_name,
            credential,
            clock: clock.unwrap_or_else(|| Arc::new(system_now)),
        })
    }

    /// The Azure Table coverage catalog for this process.
    #[must_use]
    pub fn catalog(&self) -> &Arc<AzureTableCatalogRepository> {
        &self.catalog
    }

    /// The blob service URL this runtime was built from.
    #[must_use]
    pub fn blob_url(&self) -> &str {
        &self.blob_url
    }

    /// The storage container name this runtime publishes into.
    #[must_use]
    pub fn container_name(&self) -> &str {
        &self.container_name
    }

    /// The durable job repository for this process.
    #[must_use]
    pub fn jobs(&self) -> &Arc<dyn JobRepository> {
        &self.jobs
    }

    pub fn contracts(&self) -> Arc<dyn lake_catalog::ContractRepository> {
        self.contracts.clone()
    }

    pub fn option_resolver(self: &Arc<Self>) -> lake_http::state::OptionResolver {
        let runtime = self.clone();
        Arc::new(move |selector| {
            let runtime = runtime.clone();
            Box::pin(async move {
                runtime
                    .require_provider_credentials()
                    .map_err(|e| DataError::Io(e.to_string()))?;
                let client = AlpacaClient::with_reqwest(
                    provider_config(&runtime.settings),
                    shared_governor(&runtime.settings),
                )
                .map_err(|e| DataError::Io(e.to_string()))?;
                lake_ingestion::options::resolve::OptionContractResolver::new(
                    Arc::new(client),
                    runtime.contracts(),
                )
                .resolve(&selector)
                .await
            })
        })
    }

    /// The identity-based read-URL issuer for this process.
    #[must_use]
    pub fn sas_issuer(&self) -> &Arc<AzureBlobSasIssuer> {
        &self.sas_issuer
    }

    /// The identity-based blob-read seam for this process.
    #[must_use]
    pub fn read_store(&self) -> Arc<dyn ReadableBlobStore> {
        self.read_store.clone()
    }

    /// Replace the durable job repository (test seam).
    #[must_use]
    pub fn with_jobs(mut self, jobs: Arc<dyn JobRepository>) -> Self {
        self.jobs = jobs;
        self
    }

    /// The file-query ensure callback used by the query route.
    #[must_use]
    pub fn ensure_callback(self: &Arc<Self>) -> EnsureCallback {
        let runtime = Arc::clone(self);
        Arc::new(
            move |asset_class, data_type, interval, symbol, start_date, end_date| {
                let runtime = Arc::clone(&runtime);
                Box::pin(async move {
                    let spec = EnsureSpec {
                        asset_class,
                        data_type,
                        symbol,
                        start_date,
                        end_date,
                        interval,
                    };
                    runtime.ensure_sessions(&spec).await.map_err(|error| {
                        QueryApplicationError::new(
                            "ensure_failed",
                            "coverage ensure failed",
                            Some(serde_json::json!({ "error": error.to_string() })),
                        )
                    })
                })
            },
        )
    }

    /// The ensure-route launcher that runs bounded ingest work.
    #[must_use]
    pub fn job_launcher(self: &Arc<Self>) -> EnsureJobLauncher {
        let runtime = Arc::clone(self);
        Arc::new(move |job: EnsureJob| {
            let runtime = Arc::clone(&runtime);
            Box::pin(async move {
                runtime
                    .run_job(&job)
                    .await
                    .map_err(|error| DataError::EnsureJob(error.to_string()))
            })
        })
    }

    /// Query callbacks calculate the same gap plan used by the ensure route.
    pub async fn ensure_sessions(&self, request: &EnsureSpec) -> Result<(), HostedEnsureError> {
        let gaps = requested_coverage(
            self.catalog.as_ref(),
            None,
            request,
            (self.clock)(),
            "ensure",
        )
        .map_err(|e| HostedEnsureError::new(e.to_string()))?
        .gap_plan()
        .gaps;
        self.run_job(&EnsureJob {
            job_id: lake_coverage::ensure_job_id(request)
                .map_err(|e| HostedEnsureError::new(e.to_string()))?,
            request: request.clone(),
            gaps,
        })
        .await
    }

    /// Compose Azure adapters; coverage application alone settles durable jobs.
    async fn run_job(&self, job: &EnsureJob) -> Result<(), HostedEnsureError> {
        if job.gaps.is_empty() {
            return Ok(());
        }
        self.require_provider_credentials()?;
        let client = Arc::new(
            AlpacaClient::with_reqwest(
                provider_config(&self.settings),
                shared_governor(&self.settings),
            )
            .map_err(|e| HostedEnsureError::new(e.to_string()))?,
        );
        let ingest = EnsureIngest {
            providers: EnsureProviders {
                equity: Some(client.clone()),
                crypto: Some(client.clone()),
                bars: Some(client.clone()),
                options: Some(client),
            },
            contracts: Some(self.contracts()),
            committer: Arc::new(self.leased_committer()?),
            trades: Arc::new(CatalogTradeReader::new(
                self.catalog.clone(),
                self.read_store(),
            )),
            clock: self.clock.clone(),
        };
        let jobs = self.jobs.clone();
        let job_id = job.job_id.clone();
        let clock = self.clock.clone();
        let progress: EnsureProgress = Arc::new(move |total, done, coverage| {
            if let Ok(Some(mut record)) = jobs.get(&job_id) {
                if record.state != lake_catalog::JobState::Running {
                    return;
                }
                record.progress = Some(JobProgress {
                    sessions_total: total as u64,
                    sessions_done: done as u64,
                    symbol: coverage.symbol.clone(),
                    session_date: coverage.session_date.isoformat(),
                    rows: coverage.row_count,
                    error: coverage.error_message.clone(),
                });
                record.updated_at = clock();
                let _ = jobs.upsert(&record);
            }
        });
        ingest.run(job, Some(progress)).await.map_err(Into::into)
    }

    fn require_provider_credentials(&self) -> Result<(), HostedEnsureError> {
        if self.settings.provider.api_key.is_none() || self.settings.provider.api_secret.is_none() {
            return Err(HostedEnsureError::new(
                "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET are required for hosted ingest",
            ));
        }
        Ok(())
    }

    /// The lease-protected atomic committer over the Azure blob store.
    ///
    /// The catalog instance is the same Arc the published API reads, so a
    /// commit is visible to in-process readers immediately.
    fn leased_committer(&self) -> Result<LeasedAtomicIngestCommitter, HostedEnsureError> {
        let blob_store: Arc<dyn lake_storage::BlobStore> =
            Arc::new(lake_storage::AzureBlobStore::from_account_url(
                &self.blob_url,
                &self.container_name,
                Arc::clone(&self.credential),
            )?);
        let leases: Arc<dyn LeaseProvider> =
            Arc::new(lake_storage::AzureBlobLeaseProvider::from_account_url(
                &self.blob_url,
                &self.container_name,
                Arc::clone(&self.credential),
                60,
                "locks",
            )?);
        let catalog: Arc<dyn CatalogRepository> = self.catalog.clone();
        let inner = AzureAtomicIngestCommitter::new(blob_store, catalog);
        Ok(LeasedAtomicIngestCommitter::new(Arc::new(inner), leases))
    }
}

/// One process-wide adaptive request governor per API key.
fn shared_governor(settings: &LakeSettings) -> Arc<RequestGovernor> {
    static GOVERNOR: OnceLock<Arc<RequestGovernor>> = OnceLock::new();
    Arc::clone(
        GOVERNOR
            .get_or_init(|| RequestGovernor::new(settings.provider.request.requests_per_minute)),
    )
}

fn provider_config(settings: &LakeSettings) -> ProviderConfig {
    let mut config = ProviderConfig::new(
        "https://data.alpaca.markets",
        settings.provider.api_key.clone(),
        settings.provider.api_secret.clone(),
    );
    config.max_concurrency = settings.provider.request.max_concurrency as usize;
    config.timeout_seconds = settings.provider.request.timeout_seconds;
    config.max_attempts = settings.provider.retry.max_attempts;
    config.honor_retry_after = settings.provider.retry.honor_retry_after;
    config.requests_per_minute = settings.provider.request.requests_per_minute;
    config
}

/// The overrides [`create_api_router`] installs when hosted configuration
/// exists, mirroring `hosted_overrides`.
pub struct HostedOverrides {
    /// The hosted coverage catalog.
    pub catalog: Arc<dyn CatalogRepository>,
    /// The hosted durable job repository.
    pub jobs: Arc<dyn JobRepository>,
    pub contracts: Arc<dyn lake_catalog::ContractRepository>,
    pub option_resolver: lake_http::state::OptionResolver,
    /// The hosted read-URL issuer.
    pub sas_issuer: Arc<dyn FileUrlIssuer>,
    /// The hosted blob-read seam.
    pub read_store: Arc<dyn ReadableBlobStore>,
    /// The query-route ensure callback.
    pub ensure_callback: EnsureCallback,
    /// The ensure-route launcher.
    pub job_launcher: EnsureJobLauncher,
}

/// Return the hosted overrides when hosted configuration exists.
///
/// `None` mirrors the Python empty-override result: no
/// `LAKE_STORAGE_ACCOUNT_NAME` means the caller falls back to the framework
/// defaults.
pub fn hosted_overrides(
    environ: &BTreeMap<String, String>,
    clock: Option<Arc<dyn Fn() -> DateTime + Send + Sync>>,
) -> Result<Option<HostedOverrides>, HostedEnsureError> {
    if environment_value(environ, STORAGE_ACCOUNT_ENV).is_none() {
        return Ok(None);
    }
    let runtime = Arc::new(HostedRuntime::from_environment(environ, clock)?);
    let catalog: Arc<dyn CatalogRepository> = runtime.catalog.clone();
    let sas_issuer: Arc<dyn FileUrlIssuer> =
        Arc::new(AzureFileUrlIssuer(Arc::clone(runtime.sas_issuer())));
    Ok(Some(HostedOverrides {
        catalog,
        jobs: Arc::clone(runtime.jobs()),
        contracts: runtime.contracts(),
        option_resolver: runtime.option_resolver(),
        sas_issuer,
        read_store: runtime.read_store(),
        ensure_callback: runtime.ensure_callback(),
        job_launcher: runtime.job_launcher(),
    }))
}

/// Adapt the Azure SAS issuer to the query boundary's URL issuer port.
struct AzureFileUrlIssuer(Arc<AzureBlobSasIssuer>);

impl FileUrlIssuer for AzureFileUrlIssuer {
    fn issue(&self, object_path: &str) -> Result<String, DataError> {
        SasIssuer::issue(&*self.0, object_path)
    }
}
