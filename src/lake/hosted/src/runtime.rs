//! Azure catalog, durable ingestion admission, ACA reconciliation, and signed read composition. No provider clients run here.

use std::collections::BTreeMap;
use std::sync::Arc;

use lake_catalog::{CatalogRepository, JobRepository, PublicationCatalog};
use lake_configuration::{ConfigError, LakeSettings};
use lake_contracts::DataError;
use lake_coverage::{EnsureAdmission, EnsureSpec, EnsureSubmission};
use lake_ingest_jobs::{
    AcaConfig, AcaRunner, BlobJobRepository, IngestionJobs, JobStore, WorkRequest,
};
use lake_query::{EnsureCallback, FileUrlIssuer, QueryApplicationError};
use lake_storage::{
    sas_issuer_from_account_url, AzureBlobSasIssuer, AzureReadableBlobStore, ReadableBlobStore,
    SasIssuer,
};

/// The default storage container name.
pub const DEFAULT_STORAGE_CONTAINER: &str = "lake";
/// The default coverage table name.
pub const DEFAULT_COVERAGE_TABLE: &str = "coverage";

const STORAGE_ACCOUNT_ENV: &str = "LAKE_STORAGE_ACCOUNT_NAME";
const STORAGE_CONTAINER_ENV: &str = "LAKE_STORAGE_CONTAINER";
const COVERAGE_TABLE_ENV: &str = "LAKE_COVERAGE_TABLE";

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
    catalog: Arc<PublicationCatalog>,
    jobs: Arc<dyn JobRepository>,
    contracts: Arc<dyn lake_catalog::ContractRepository>,
    sas_issuer: Arc<AzureBlobSasIssuer>,
    read_store: Arc<AzureReadableBlobStore>,
    blob_url: String,
    container_name: String,
    ingestion: Arc<IngestionJobs>,
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

impl HostedRuntime {
    /// Build the runtime from non-secret environment configuration.
    pub fn from_environment(environ: &BTreeMap<String, String>) -> Result<Self, HostedEnsureError> {
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
        let contracts = Arc::new(lake_catalog::AzureTableContractRepository::new(Arc::new(
            lake_catalog::SdkTableEntityStore::from_account_url(
                &table_url,
                &coverage_table,
                Arc::clone(&credential),
            )?,
        )));
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
        let jobs: Arc<dyn JobRepository> = Arc::new(BlobJobRepository::new(read_store.clone()));
        let catalog = PublicationCatalog::new(
            read_store.clone(),
            Arc::new(lake_storage::AzureBlobLeaseProvider::from_account_url(
                &blob_url,
                &container_name,
                credential.clone(),
                60,
                lake_catalog::PUBLICATION_PREFIX,
            )?),
        );
        let ingestion = Arc::new(IngestionJobs {
            store: JobStore::new(format!("{blob_url}/{container_name}"), credential.clone())?,
            runner: AcaRunner::new(AcaConfig::from_environment(environ)?, credential.clone())?,
        });
        ingestion
            .clone()
            .start_reconciler(std::time::Duration::from_secs(2));
        Ok(Self {
            catalog: Arc::new(catalog),
            jobs,
            contracts,
            sas_issuer,
            read_store,
            blob_url,
            container_name,
            ingestion,
        })
    }

    /// The committed manifest coverage catalog for this process.
    #[must_use]
    pub fn catalog(&self) -> &Arc<PublicationCatalog> {
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
        options(self.ingestion.clone())
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

    /// The file-query ensure callback used by the query route.
    #[must_use]
    pub fn ensure_callback(self: &Arc<Self>) -> EnsureCallback {
        query_ensure(self.ingestion.clone())
    }

    /// Persist work; the independent reconciler dispatches the retained job to ACA.
    pub fn job_admission(self: &Arc<Self>) -> EnsureAdmission {
        admission(self.ingestion.clone())
    }
}

pub(crate) fn query_ensure<
    R: lake_ingest_jobs::WorkerRunner,
    S: lake_ingest_jobs::JobPersistence,
>(
    jobs: Arc<IngestionJobs<R, S>>,
) -> EnsureCallback {
    Arc::new(
        move |asset_class, data_type, interval, symbol, start_date, end_date| {
            let jobs = jobs.clone();
            Box::pin(async move {
                let spec = EnsureSpec {
                    asset_class,
                    data_type,
                    symbol,
                    start_date,
                    end_date,
                    interval,
                };
                let id = lake_coverage::ensure_job_id(&spec).map_err(|e| {
                    QueryApplicationError::new("invalid_request", e.to_string(), None)
                })?;
                let job = jobs
                    .submit(id, vec![work_request(&spec)])
                    .await
                    .map_err(|e| {
                        QueryApplicationError::new("ensure_failed", e.to_string(), None)
                    })?;
                tokio::spawn(async move {
                    if let Err(e) = jobs.reconcile().await {
                        eprintln!("ingestion dispatch failed: {e}");
                    }
                });
                match job.record.state {
                    lake_catalog::JobState::Complete => Ok(()),
                    lake_catalog::JobState::Failed => Err(QueryApplicationError::new(
                        "ensure_failed",
                        "ingestion job failed",
                        None,
                    )),
                    _ => Err(QueryApplicationError::new(
                        "ingestion_pending",
                        format!("wait for ingestion job {}", job.record.job_id),
                        None,
                    )),
                }
            })
        },
    )
}

fn work_request(spec: &EnsureSpec) -> WorkRequest {
    let spec = spec.canonical();
    WorkRequest::Dataset {
        asset_class: spec.asset_class.value().into(),
        data_type: spec.data_type.value().into(),
        symbol: spec.symbol,
        start_date: spec.start_date.isoformat(),
        end_date: spec.end_date.isoformat(),
        interval: spec.interval,
    }
}

/// The overrides [`crate::api::create_api_router`] installs when hosted configuration
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
    pub job_admission: EnsureAdmission,
}

/// Compose required Azure adapters for the harness market-data capability.
pub fn hosted_overrides(
    environ: &BTreeMap<String, String>,
) -> Result<HostedOverrides, HostedEnsureError> {
    let runtime = Arc::new(HostedRuntime::from_environment(environ)?);
    let catalog: Arc<dyn CatalogRepository> = runtime.catalog.clone();
    let sas_issuer: Arc<dyn FileUrlIssuer> =
        Arc::new(AzureFileUrlIssuer(Arc::clone(runtime.sas_issuer())));
    Ok(HostedOverrides {
        catalog,
        jobs: Arc::clone(runtime.jobs()),
        contracts: runtime.contracts(),
        option_resolver: runtime.option_resolver(),
        sas_issuer,
        read_store: runtime.read_store(),
        ensure_callback: runtime.ensure_callback(),
        job_admission: runtime.job_admission(),
    })
}

/// Adapt the Azure SAS issuer to the query boundary's URL issuer port.
struct AzureFileUrlIssuer(Arc<AzureBlobSasIssuer>);

impl FileUrlIssuer for AzureFileUrlIssuer {
    fn issue(&self, object_path: &str) -> Result<String, DataError> {
        SasIssuer::issue(&*self.0, object_path)
    }
}

pub(crate) fn options<R: lake_ingest_jobs::WorkerRunner, S: lake_ingest_jobs::JobPersistence>(
    jobs: Arc<IngestionJobs<R, S>>,
) -> lake_http::state::OptionResolver {
    Arc::new(move |selector, explicit_id| {
        let jobs = jobs.clone();
        Box::pin(async move {
            use sha2::{Digest, Sha256};
            let encoded =
                serde_json::to_vec(&selector).map_err(|e| DataError::value(e.to_string()))?;
            let id = explicit_id
                .unwrap_or_else(|| format!("option-{}", hex::encode(Sha256::digest(encoded))));
            let job = jobs
                .submit(id, vec![WorkRequest::OptionSelection { selector }])
                .await?;
            if !job.terminal() {
                let jobs = jobs.clone();
                tokio::spawn(async move {
                    if let Err(e) = jobs.reconcile().await {
                        eprintln!("ingestion dispatch failed: {e}");
                    }
                });
            }
            match job.record.state {
                lake_catalog::JobState::Complete => serde_json::from_value(
                    job.result
                        .ok_or_else(|| DataError::value("option job completed without a result"))?,
                )
                .map_err(|e| DataError::value(e.to_string())),
                lake_catalog::JobState::Failed => Err(DataError::EnsureJob(
                    job.record
                        .error_message
                        .unwrap_or_else(|| "option acquisition failed".into()),
                )),
                _ => Err(DataError::PendingJob(job.record.job_id)),
            }
        })
    })
}

pub(crate) fn admission<R: lake_ingest_jobs::WorkerRunner, S: lake_ingest_jobs::JobPersistence>(
    jobs: Arc<IngestionJobs<R, S>>,
) -> EnsureAdmission {
    Arc::new(move |job: EnsureSubmission| {
        let jobs = jobs.clone();
        Box::pin(async move {
            let requests = job.requests.iter().map(work_request).collect();
            jobs.submit(job.job_id, requests).await?;
            tokio::spawn(async move {
                if let Err(e) = jobs.reconcile().await {
                    eprintln!("ingestion dispatch failed: {e}");
                }
            });
            Ok(())
        })
    })
}
