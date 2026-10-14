//! On-demand local ensure execution backed by complete Alpaca fetches.
//!
//! Ports `lake/local/ensure.py`: maps one ensure job onto the arbitrary-symbol
//! equity or crypto backfill so a coverage request for a symbol the local lake
//! has never ingested triggers a real fetch instead of a no-op.

use std::collections::BTreeMap;
use std::sync::Arc;

use futures_util::future::BoxFuture;
use lake_catalog::{JobProgress, JobRepository, JobState, JsonCatalogStore};
use lake_configuration::{LakeSettings, UniverseSettings};
use lake_contracts::{AssetClass, DataError, DataType, DateTime};
use lake_coverage::{EnsureJob, EnsureJobLauncher};
use lake_ingestion::{
    BarsDataProvider, CatalogTradeReader, CryptoDataProvider, EnsureIngest, EnsureProgress,
    EnsureProviders, EquityDataProvider,
};
use lake_providers::AlpacaClient;
use lake_storage::LocalFilesystemStore;

use crate::ingest::{provider_config, shared_governor, LocalIngestError};
use crate::state::{lookup, LocalLayout};

/// The ensure route waits this long inside the request before returning 202.
///
/// Local fetches are long, so the wait is deliberately short: the launcher runs
/// on the runtime, the request returns promptly, and the caller polls the
/// catalog coverage until the requested sessions are terminal.
pub const DEFAULT_ENSURE_DEADLINE_SECONDS: f64 = 10.0;

const DEADLINE_ENV: &str = "LAKE_LOCAL_ENSURE_DEADLINE_SECONDS";

/// Return the configured ensure wait deadline for the local API.
pub fn local_ensure_deadline_seconds(
    environ: &BTreeMap<String, String>,
) -> Result<f64, LocalIngestError> {
    let Some(raw) = lookup(environ, DEADLINE_ENV) else {
        return Ok(DEFAULT_ENSURE_DEADLINE_SECONDS);
    };
    if raw.trim().is_empty() {
        return Ok(DEFAULT_ENSURE_DEADLINE_SECONDS);
    }
    let value: f64 = raw.trim().parse().map_err(|_| {
        LocalIngestError::new("LAKE_LOCAL_ENSURE_DEADLINE_SECONDS must be a number")
    })?;
    if value <= 0.0 {
        return Err(LocalIngestError::new(
            "LAKE_LOCAL_ENSURE_DEADLINE_SECONDS must be positive",
        ));
    }
    Ok(value)
}

/// Run a local ensure request as a bounded provider fetch.
///
/// The launcher satisfies the published [`EnsureJobLauncher`] contract: the
/// ensure route invokes it and persists the resulting coverage in the local
/// catalog.
pub struct LocalEnsureLauncher {
    layout: LocalLayout,
    settings: LakeSettings,
    _universes: UniverseSettings,
    equity: Option<Arc<dyn EquityDataProvider>>,
    crypto: Option<Arc<dyn CryptoDataProvider>>,
    bars: Option<Arc<dyn BarsDataProvider>>,
    options: Option<Arc<dyn lake_ingestion::options::transaction::OptionMarketDataClient>>,
    contracts: Option<Arc<dyn lake_catalog::ContractRepository>>,
    catalog: Option<Arc<JsonCatalogStore>>,
    jobs: Option<Arc<dyn JobRepository>>,
    now: DateTime,
}

impl LocalEnsureLauncher {
    /// Build a launcher over the local layout and loaded configuration.
    pub fn new(
        layout: LocalLayout,
        _environ: BTreeMap<String, String>,
        settings: LakeSettings,
        universes: UniverseSettings,
        now: DateTime,
    ) -> Result<Self, LocalIngestError> {
        Ok(Self {
            layout,
            settings,
            _universes: universes,
            equity: None,
            crypto: None,
            bars: None,
            options: None,
            contracts: None,
            catalog: None,
            jobs: None,
            now,
        })
    }

    /// Install injected providers; absent providers are built from credentials.
    #[must_use]
    pub fn with_providers(
        mut self,
        equity: Option<Arc<dyn EquityDataProvider>>,
        crypto: Option<Arc<dyn CryptoDataProvider>>,
    ) -> Self {
        self.equity = equity;
        self.crypto = crypto;
        self
    }

    /// Install an injected bars provider; absent builds from credentials.
    #[must_use]
    pub fn with_bars_provider(mut self, bars: Option<Arc<dyn BarsDataProvider>>) -> Self {
        self.bars = bars;
        self
    }

    pub fn with_options(
        mut self,
        provider: Option<Arc<dyn lake_ingestion::options::transaction::OptionMarketDataClient>>,
        contracts: Arc<dyn lake_catalog::ContractRepository>,
    ) -> Self {
        self.options = provider;
        self.contracts = Some(contracts);
        self
    }

    /// Reuse the process's catalog store so commits are visible to in-process
    /// readers immediately instead of through a file re-read.
    #[must_use]
    pub fn with_catalog(mut self, catalog: Arc<JsonCatalogStore>) -> Self {
        self.catalog = Some(catalog);
        self
    }

    /// Reuse the process's job store so incremental progress is written onto
    /// the durable job record as each session finishes.
    #[must_use]
    pub fn with_jobs(mut self, jobs: Arc<dyn JobRepository>) -> Self {
        self.jobs = Some(jobs);
        self
    }

    /// Build the per-job progress sink that mirrors walk progress onto the
    /// durable job record.
    fn progress_sink(&self, job_id: &str) -> Option<EnsureProgress> {
        let jobs = self.jobs.clone()?;
        let job_id = job_id.to_string();
        let now = self.now;
        Some(Arc::new(
            move |total, done, coverage: &lake_catalog::CoverageRecord| {
                let Ok(Some(mut record)) = jobs.get(&job_id) else {
                    return;
                };
                if record.state != JobState::Running {
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
                record.updated_at = now;
                let _ = jobs.upsert(&record);
            },
        ))
    }

    /// Build the callable launcher the ensure route invokes.
    pub fn launcher(self: &Arc<Self>) -> EnsureJobLauncher {
        let launcher = Arc::clone(self);
        Arc::new(move |job: EnsureJob| {
            let launcher = Arc::clone(&launcher);
            Box::pin(async move {
                launcher
                    .run(job)
                    .await
                    .map_err(|error| DataError::EnsureJob(error.message))
            }) as BoxFuture<'static, Result<(), DataError>>
        })
    }

    /// Compose filesystem adapters around the shared ensure application.
    pub(crate) async fn run(&self, job: EnsureJob) -> Result<(), LocalIngestError> {
        if job.gaps.is_empty() {
            return Ok(());
        }
        let request = &job.request;
        let mut providers = EnsureProviders {
            equity: self.equity.clone(),
            crypto: self.crypto.clone(),
            bars: self.bars.clone(),
            options: self.options.clone(),
        };
        let missing_provider = if request.data_type == DataType::Bars {
            providers.bars.is_none()
        } else {
            match request.asset_class {
                AssetClass::Equity => providers.equity.is_none(),
                AssetClass::Crypto => providers.crypto.is_none(),
                AssetClass::Option => providers.options.is_none(),
                _ => return Err(LocalIngestError::new("unsupported ensure asset class")),
            }
        };
        if missing_provider {
            if self.settings.provider.api_key.is_none()
                || self.settings.provider.api_secret.is_none()
            {
                return Err(LocalIngestError::new(
                    "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET are required for ingest",
                ));
            }
            let client = Arc::new(
                AlpacaClient::with_reqwest(
                    provider_config(&self.settings),
                    shared_governor(&self.settings),
                )
                .map_err(|e| LocalIngestError::new(e.to_string()))?,
            );
            if request.data_type == DataType::Bars {
                providers.bars = Some(client);
            } else {
                match request.asset_class {
                    AssetClass::Equity => providers.equity = Some(client),
                    AssetClass::Crypto => providers.crypto = Some(client),
                    AssetClass::Option => providers.options = Some(client),
                    _ => unreachable!("validated asset class"),
                }
            }
        }
        self.layout.initialize()?;
        let catalog = match &self.catalog {
            Some(catalog) => catalog.clone(),
            None => Arc::new(JsonCatalogStore::open(self.layout.catalog_path())?),
        };
        let store = Arc::new(LocalFilesystemStore::new(self.layout.objects()));
        let ingest = EnsureIngest {
            providers,
            contracts: self.contracts.clone(),
            committer: Arc::new(crate::LocalIngestCommitter::new(
                store.clone(),
                catalog.clone(),
            )),
            trades: Arc::new(CatalogTradeReader::new(catalog, store)),
            clock: {
                let now = self.now;
                Arc::new(move || now)
            },
        };
        ingest
            .run(&job, self.progress_sink(&job.job_id))
            .await
            .map_err(Into::into)
    }
}
