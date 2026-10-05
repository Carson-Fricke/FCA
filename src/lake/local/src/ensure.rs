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
use lake_contracts::{AssetClass, BarInterval, DataError, DataType, DateTime};
use lake_coverage::{EnsureJob, EnsureJobLauncher, EnsureSpec};
use lake_ingestion::{BarsDataProvider, CryptoDataProvider, EquityDataProvider};

use crate::ingest::{
    run_local_backfill, run_local_bars_backfill, run_local_crypto_backfill, BackfillProgress,
    BackfillProgressSink, LocalBackfillOptions, LocalBarsBackfillOptions,
    LocalCryptoBackfillOptions, LocalIngestError,
};
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
    environ: BTreeMap<String, String>,
    settings: LakeSettings,
    _universes: UniverseSettings,
    equity: Option<Arc<dyn EquityDataProvider>>,
    crypto: Option<Arc<dyn CryptoDataProvider>>,
    bars: Option<Arc<dyn BarsDataProvider>>,
    catalog: Option<Arc<JsonCatalogStore>>,
    jobs: Option<Arc<dyn JobRepository>>,
    now: DateTime,
}

impl LocalEnsureLauncher {
    /// Build a launcher over the local layout and loaded configuration.
    pub fn new(
        layout: LocalLayout,
        environ: BTreeMap<String, String>,
        settings: LakeSettings,
        universes: UniverseSettings,
        now: DateTime,
    ) -> Result<Self, LocalIngestError> {
        Ok(Self {
            layout,
            environ,
            settings,
            _universes: universes,
            equity: None,
            crypto: None,
            bars: None,
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
    fn progress_sink(&self, job_id: &str) -> Option<BackfillProgressSink> {
        let jobs = self.jobs.clone()?;
        let job_id = job_id.to_string();
        let updated_at = self.now;
        Some(Arc::new(move |progress: BackfillProgress| {
            let Ok(Some(mut record)) = jobs.get(&job_id) else {
                return;
            };
            if record.state != JobState::Running {
                return;
            }
            record.progress = Some(JobProgress {
                sessions_total: progress.sessions_total as u64,
                sessions_done: progress.sessions_done as u64,
                symbol: progress.symbol,
                session_date: progress.session_date,
                rows: progress.data_type_rows.values().copied().sum(),
                error: progress.error,
            });
            record.updated_at = updated_at;
            let _ = jobs.upsert(&record);
        }))
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

    /// Run one ensure request as a bounded backfill.
    pub(crate) async fn run(&self, job: EnsureJob) -> Result<(), LocalIngestError> {
        let request = job.request;
        let progress = self.progress_sink(&job.job_id);
        if request.data_type == DataType::Bars {
            return self.run_bars(&request, progress).await;
        }
        match request.asset_class {
            AssetClass::Equity => run_local_backfill(LocalBackfillOptions {
                layout: self.layout.clone(),
                symbols: vec![request.symbol.clone()],
                start_date: request.start_date,
                end_date: request.end_date,
                // One ensure asks for one data type and fetches only that one.
                data_types: Some(vec![request.data_type]),
                environ: self.environ.clone(),
                now: self.now,
                settings: self.settings.clone(),
                provider: self.equity.clone(),
                retry_settle_seconds: None,
                catalog: self.catalog.clone(),
                progress: progress.clone(),
            })
            .await
            .map(|_| ()),
            AssetClass::Crypto => run_local_crypto_backfill(LocalCryptoBackfillOptions {
                layout: self.layout.clone(),
                pairs: vec![request.symbol.clone()],
                start_date: request.start_date,
                end_date: request.end_date,
                data_types: Some(vec![request.data_type]),
                environ: self.environ.clone(),
                now: self.now,
                settings: self.settings.clone(),
                provider: self.crypto.clone(),
                retry_settle_seconds: None,
                catalog: self.catalog.clone(),
                progress,
            })
            .await
            .map(|_| ()),
            _ => Err(LocalIngestError::new(
                "options ingest is not supported by the local launcher",
            )),
        }
    }

    /// Run one bars ensure: fetch the window's bars only.
    ///
    /// A bars ensure fetches exactly the requested data type and never
    /// backfills trades.  The bars backfill prefers provider bars and derives
    /// only the sessions the provider did not cover from the stored trade
    /// partitions when they exist; sessions without stored trades publish
    /// error coverage instead of triggering a trade fetch the caller did not
    /// request.
    async fn run_bars(
        &self,
        request: &EnsureSpec,
        progress: Option<BackfillProgressSink>,
    ) -> Result<(), LocalIngestError> {
        if !matches!(request.asset_class, AssetClass::Equity | AssetClass::Crypto) {
            return Err(LocalIngestError::new(
                "options ingest is not supported by the local launcher",
            ));
        }
        let Some(token) = request.interval.as_deref() else {
            return Err(LocalIngestError::new("bars requires an interval"));
        };
        let interval =
            BarInterval::parse(token).map_err(|error| LocalIngestError::new(error.to_string()))?;
        run_local_bars_backfill(LocalBarsBackfillOptions {
            layout: self.layout.clone(),
            asset_class: request.asset_class,
            symbols: vec![request.symbol.trim().to_uppercase()],
            interval,
            start_date: request.start_date,
            end_date: request.end_date,
            environ: self.environ.clone(),
            now: self.now,
            settings: self.settings.clone(),
            provider: self.bars.clone(),
            retry_settle_seconds: None,
            catalog: self.catalog.clone(),
            progress,
        })
        .await
        .map(|_| ())
    }
}
