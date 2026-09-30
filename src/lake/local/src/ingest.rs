//! Alpaca ingestion composed over local filesystem adapters.
//!
//! Ports `lake/local/ingest.py`: one closed equity session into deterministic
//! local state, the arbitrary-symbol equity backfill that resumes from the
//! catalog and repairs transient failures in-run, and the UTC-day crypto
//! backfill. Credentials come from the loaded [`LakeSettings`], so a missing
//! provider key fails before any provider request or local-state mutation.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use futures_util::future::{join_all, BoxFuture};
use lake_catalog::{
    CatalogRepository, CoverageQuery, CoverageRecord, CoverageStatus, JsonCatalogStore,
};
use lake_configuration::{ConfigError, LakeSettings, UniverseSettings};
use lake_contracts::{
    canonical_symbol, crypto_sessions, symbol_filename, xnys_sessions, AssetClass, BarInterval,
    DataError, DataType, Date, DateTime,
};
use lake_coverage::{calculate_coverage, CoverageSession};
use lake_ingestion::{
    AtomicIngestCommitter, BarsDataProvider, BarsIngestResult, BarsIngestTransaction,
    CatalogTradeReader, CryptoDataProvider, CryptoIngestResult, CryptoIngestTransaction,
    EquityDataProvider, EquityIngestResult, EquityIngestTransaction, StagingPartitionWriter,
};
use lake_providers::{AlpacaClient, ProviderConfig, RequestGovernor};
use lake_storage::{BlobStore, LocalFilesystemStore, ReadableBlobStore};
use serde::Serialize;
use tokio::sync::Semaphore;

use crate::committer::{LocalBackfillCommitter, LocalIngestCommitter};
use crate::state::{LocalLayout, LocalStateError};

/// Sessions a default ingest searches backwards for a closed XNYS session.
const DEFAULT_LOOKBACK_DAYS: i32 = 31;

/// Bounded concurrent per-session fetches for a backfill.
// Sessions may run in parallel; the lane's adaptive bound, not the session
// count, is the provider-facing limiter, and parallel session streams beat a
// single stream on trade pages (measured).
const BACKFILL_CONCURRENCY: usize = 4;

/// Coverage rows buffered before one catalog flush.
const CATALOG_FLUSH_RECORDS: usize = 64;

/// Settle delay before the one in-run retry pass over transiently-failed
/// sessions.
const RETRY_SETTLE_SECONDS: f64 = 2.0;

/// A supported local ingest could not be started or completed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LocalIngestError {
    /// Human-readable failure detail.
    pub message: String,
}

impl LocalIngestError {
    /// Build an error with the supplied detail.
    pub(crate) fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for LocalIngestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for LocalIngestError {}

impl From<DataError> for LocalIngestError {
    fn from(error: DataError) -> Self {
        Self {
            message: error.to_string(),
        }
    }
}

impl From<LocalStateError> for LocalIngestError {
    fn from(error: LocalStateError) -> Self {
        Self {
            message: error.message,
        }
    }
}

impl From<ConfigError> for LocalIngestError {
    fn from(error: ConfigError) -> Self {
        Self {
            message: error.to_string(),
        }
    }
}

/// One published coverage row summary in an ingest result.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct IngestCoverageEntry {
    /// Data type of the covered partition.
    pub data_type: String,
    /// Rows written for this symbol-day.
    pub rows: u64,
    /// Terminal or replaceable coverage outcome.
    pub status: String,
}

/// One published object summary in an ingest result.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct IngestObjectEntry {
    /// Bytes written for this symbol-day.
    pub bytes: u64,
    /// Canonical Hive object path.
    pub path: String,
    /// Rows written for this symbol-day.
    pub rows: u64,
}

/// Result of one local ingest, serialized with the Python JSON keys.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct IngestResult {
    /// Path of the local coverage catalog.
    pub catalog: String,
    /// One entry per published data type.
    pub coverage: Vec<IngestCoverageEntry>,
    /// Local data root the objects were written below.
    pub data_root: String,
    /// One entry per published Parquet object.
    pub objects: Vec<IngestObjectEntry>,
    /// Session date the ingest published.
    pub session_date: String,
    /// Canonical uppercase symbol.
    pub symbol: String,
}

/// Inputs for one local ingest.
pub struct LocalIngestOptions {
    /// Local layout the ingest writes into.
    pub layout: LocalLayout,
    /// Symbol to fetch; the first soak universe symbol when absent.
    pub symbol: Option<String>,
    /// Session to fetch; the latest closed XNYS session when absent.
    pub session_date: Option<Date>,
    /// Environment overrides used for credential checks and provider config.
    pub environ: BTreeMap<String, String>,
    /// Injected current instant used for session selection.
    pub now: DateTime,
    /// Configured soak universe providing the default symbol.
    pub universes: UniverseSettings,
    /// Loaded lake settings providing provider credentials.
    pub settings: LakeSettings,
    /// Injected equity provider; otherwise built from the provider settings.
    pub provider: Option<Arc<dyn EquityDataProvider>>,
    /// Shared catalog store; the layout's file is opened when absent. Callers
    /// that already hold the process's store pass it here so in-process
    /// readers see commits without waiting on a file re-read.
    pub catalog: Option<Arc<JsonCatalogStore>>,
}

/// One session entry in a backfill result.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct SessionIngestEntry {
    /// Rows written per data type for this session.
    pub data_type_rows: BTreeMap<String, u64>,
    /// Failure detail when the session failed.
    pub error: Option<String>,
    /// Whether the failure was a transient fetch error the run retried.
    pub retryable: bool,
    /// Session date the entry describes.
    pub session_date: String,
    /// Coverage status per published data type, in publication order.
    pub statuses: Vec<String>,
    /// Canonical uppercase symbol.
    pub symbol: String,
}

/// Result of one local equity backfill, serialized with the Python JSON keys.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct BackfillResult {
    /// Local data root the objects were written below.
    pub data_root: String,
    /// One entry per failed session.
    pub errors: Vec<SessionIngestEntry>,
    /// Inclusive last requested session date.
    pub requested_end: String,
    /// Inclusive first requested session date.
    pub requested_start: String,
    /// One entry per planned session, in symbol-then-session order.
    pub sessions: Vec<SessionIngestEntry>,
    /// Canonical uppercase symbols, deduplicated in request order.
    pub symbols: Vec<String>,
}

/// One incremental progress snapshot from a backfill walk.
#[derive(Clone, Debug)]
pub struct BackfillProgress {
    /// Sessions the walk planned.
    pub sessions_total: usize,
    /// Sessions that reached a terminal outcome so far, in completion order.
    pub sessions_done: usize,
    /// Symbol (or pair) of the session that just finished.
    pub symbol: String,
    /// Session date that just finished, ISO formatted.
    pub session_date: String,
    /// Rows committed for the finished session, by data type.
    pub data_type_rows: BTreeMap<String, u64>,
    /// Failure detail when the finished session failed.
    pub error: Option<String>,
}

/// A backfill walk's incremental progress observer.
pub type BackfillProgressSink = Arc<dyn Fn(BackfillProgress) + Send + Sync>;

/// Inputs for one local equity backfill.
pub struct LocalBackfillOptions {
    /// Local layout the backfill writes into.
    pub layout: LocalLayout,
    /// Symbols to fetch, canonicalized and deduplicated.
    pub symbols: Vec<String>,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Data types to fetch; both when absent.
    pub data_types: Option<Vec<DataType>>,
    /// Environment overrides used for credential checks and provider config.
    pub environ: BTreeMap<String, String>,
    /// Injected current instant used for session clocks.
    pub now: DateTime,
    /// Loaded lake settings providing provider credentials.
    pub settings: LakeSettings,
    /// Injected equity provider; otherwise built from the provider settings.
    pub provider: Option<Arc<dyn EquityDataProvider>>,
    /// Settle delay before the in-run retry pass; two seconds when absent.
    pub retry_settle_seconds: Option<f64>,
    /// Shared catalog store; the layout's file is opened when absent. Callers
    /// that already hold the process's store pass it here so in-process
    /// readers see commits without waiting on a file re-read.
    pub catalog: Option<Arc<JsonCatalogStore>>,
    /// Incremental progress observer; each finished session reports once, as
    /// it finishes, so a long window's caching progress is visible.
    pub progress: Option<BackfillProgressSink>,
}

/// Result of one local crypto backfill, serialized with the Python JSON keys.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct CryptoBackfillResult {
    /// Always `crypto`.
    pub asset_class: String,
    /// Local data root the objects were written below.
    pub data_root: String,
    /// One entry per failed day.
    pub errors: Vec<SessionIngestEntry>,
    /// Inclusive last requested day.
    pub requested_end: String,
    /// Inclusive first requested day.
    pub requested_start: String,
    /// One entry per planned pair-day, in pair-then-day order.
    pub sessions: Vec<SessionIngestEntry>,
    /// Canonical uppercase pairs, deduplicated in request order.
    pub symbols: Vec<String>,
}

/// Inputs for one local crypto backfill.
pub struct LocalCryptoBackfillOptions {
    /// Local layout the backfill writes into.
    pub layout: LocalLayout,
    /// Pairs to fetch, canonicalized and deduplicated.
    pub pairs: Vec<String>,
    /// Inclusive first UTC day.
    pub start_date: Date,
    /// Inclusive last UTC day.
    pub end_date: Date,
    /// Data types to fetch; both when absent.
    pub data_types: Option<Vec<DataType>>,
    /// Environment overrides used for credential checks and provider config.
    pub environ: BTreeMap<String, String>,
    /// Injected current instant used for the partial-day clock.
    pub now: DateTime,
    /// Loaded lake settings providing provider credentials.
    pub settings: LakeSettings,
    /// Injected crypto provider; otherwise built from the provider settings.
    pub provider: Option<Arc<dyn CryptoDataProvider>>,
    /// Settle delay before the in-run retry pass; two seconds when absent.
    pub retry_settle_seconds: Option<f64>,
    /// Shared catalog store; the layout's file is opened when absent. Callers
    /// that already hold the process's store pass it here so in-process
    /// readers see commits without waiting on a file re-read.
    pub catalog: Option<Arc<JsonCatalogStore>>,
    /// Incremental progress observer; each finished pair-day reports once, as
    /// it finishes.
    pub progress: Option<BackfillProgressSink>,
}

/// The normalized outcome of one session transaction.
struct TransactionOutcome {
    symbol: String,
    session_date: Date,
    coverage: Vec<CoverageRecord>,
    retryable: bool,
}

impl From<EquityIngestResult> for TransactionOutcome {
    fn from(result: EquityIngestResult) -> Self {
        Self {
            symbol: result.symbol,
            session_date: result.session_date,
            coverage: result.coverage,
            retryable: result.retryable,
        }
    }
}

impl From<CryptoIngestResult> for TransactionOutcome {
    fn from(result: CryptoIngestResult) -> Self {
        Self {
            symbol: result.symbol,
            session_date: result.session_date,
            coverage: result.coverage,
            retryable: result.retryable,
        }
    }
}

/// One prepared session run: a symbol or pair plus its session window.
type SessionRun = Arc<
    dyn Fn(String, CoverageSession) -> BoxFuture<'static, Result<TransactionOutcome, DataError>>
        + Send
        + Sync,
>;

/// Everything a backfill walk needs after provider and session preparation.
struct BackfillPlan {
    layout: LocalLayout,
    catalog: Arc<JsonCatalogStore>,
    symbols: Vec<String>,
    sessions: Vec<CoverageSession>,
    selected: Vec<DataType>,
    start_date: Date,
    end_date: Date,
    committer: Arc<LocalBackfillCommitter>,
    retry_settle_seconds: Option<f64>,
    progress: Option<BackfillProgressSink>,
}

/// The walk output shared by the equity and crypto results.
struct BackfillOutput {
    data_root: String,
    errors: Vec<SessionIngestEntry>,
    requested_end: String,
    requested_start: String,
    sessions: Vec<SessionIngestEntry>,
    symbols: Vec<String>,
}

/// Fetch one closed equity session into deterministic local state.
pub async fn run_local_ingest(
    options: LocalIngestOptions,
) -> Result<IngestResult, LocalIngestError> {
    require_credentials(&options.settings, "ingest")?;
    let symbol = match &options.symbol {
        Some(symbol) => symbol.trim().to_uppercase(),
        None => options
            .universes
            .equities
            .soak
            .default_symbol()
            .map_err(LocalIngestError::from)?,
    };
    let session = select_session(options.session_date, options.now)?;
    options.layout.initialize()?;

    let catalog: Arc<JsonCatalogStore> = match options.catalog.clone() {
        Some(catalog) => catalog,
        None => Arc::new(JsonCatalogStore::open(options.layout.catalog_path())?),
    };
    let repository: Arc<dyn CatalogRepository> = catalog.clone();
    let store: Arc<dyn BlobStore> = Arc::new(LocalFilesystemStore::new(options.layout.objects()));
    let committer = Arc::new(LocalIngestCommitter::new(store, repository));
    let provider = equity_provider(options.provider, &options.settings)?;
    let transaction =
        EquityIngestTransaction::new(provider, Arc::new(StagingPartitionWriter::new()), committer);
    let result = transaction.run(&symbol, &session, None).await?;

    if let Some(record) = result
        .coverage
        .iter()
        .find(|record| record.status == CoverageStatus::Error)
    {
        let detail = record
            .error_message
            .clone()
            .unwrap_or_else(|| "provider request failed".to_string());
        return Err(LocalIngestError::new(format!(
            "Alpaca ingest failed: {detail}"
        )));
    }
    Ok(IngestResult {
        catalog: options.layout.catalog_path().to_string_lossy().into_owned(),
        coverage: result
            .coverage
            .iter()
            .map(|record| IngestCoverageEntry {
                data_type: record.data_type.value().to_string(),
                rows: record.row_count,
                status: record.status.value().to_string(),
            })
            .collect(),
        data_root: options.layout.root().to_string_lossy().into_owned(),
        objects: result
            .objects
            .iter()
            .map(|metadata| IngestObjectEntry {
                bytes: metadata.bytes,
                path: metadata.path.clone(),
                rows: metadata.rows,
            })
            .collect(),
        session_date: result.session_date.isoformat(),
        symbol: result.symbol,
    })
}

/// Fetch every closed equity session an arbitrary symbol set still misses.
///
/// The symbol set is not restricted to the configured soak universe. Sessions
/// with terminal coverage for every requested data type are skipped, so a
/// repeated ensure resumes from the catalog instead of refetching. Per-session
/// failures are recorded as error coverage and surfaced in the result; the
/// function raises only when no session could be processed.
pub async fn run_local_backfill(
    options: LocalBackfillOptions,
) -> Result<BackfillResult, LocalIngestError> {
    let symbols = normalize_symbols(&options.symbols)?;
    let selected = normalize_data_types(options.data_types.as_deref(), false)?;
    if options.end_date < options.start_date {
        return Err(LocalIngestError::new(
            "start_date must be on or before end_date",
        ));
    }
    require_credentials(&options.settings, "backfill")?;
    options.layout.initialize()?;

    let catalog: Arc<JsonCatalogStore> = match options.catalog.clone() {
        Some(catalog) => catalog,
        None => Arc::new(JsonCatalogStore::open(options.layout.catalog_path())?),
    };
    let store: Arc<dyn BlobStore> = Arc::new(LocalFilesystemStore::new(options.layout.objects()));
    let committer = Arc::new(LocalBackfillCommitter::new(
        store,
        Arc::clone(&catalog),
        CATALOG_FLUSH_RECORDS,
    ));
    let provider = equity_provider(options.provider, &options.settings)?;
    let sessions: Vec<CoverageSession> = xnys_sessions(options.start_date, options.end_date)?
        .iter()
        .map(|session| CoverageSession::from_exchange(session, options.now))
        .collect();

    let run: SessionRun = {
        let provider = Arc::clone(&provider);
        let committer = Arc::clone(&committer);
        let selected = selected.clone();
        Arc::new(move |symbol, session| {
            let provider = Arc::clone(&provider);
            let committer: Arc<dyn AtomicIngestCommitter> = committer.clone();
            let selected = selected.clone();
            Box::pin(async move {
                let transaction = EquityIngestTransaction::new(
                    provider,
                    Arc::new(StagingPartitionWriter::new()),
                    committer,
                );
                let result = transaction.run(&symbol, &session, Some(&selected)).await?;
                Ok(TransactionOutcome::from(result))
            })
        })
    };

    let output = execute_backfill(
        BackfillPlan {
            layout: options.layout,
            catalog,
            symbols,
            sessions,
            selected,
            start_date: options.start_date,
            end_date: options.end_date,
            committer,
            retry_settle_seconds: options.retry_settle_seconds,
            progress: options.progress,
        },
        run,
    )
    .await?;
    Ok(BackfillResult {
        data_root: output.data_root,
        errors: output.errors,
        requested_end: output.requested_end,
        requested_start: output.requested_start,
        sessions: output.sessions,
        symbols: output.symbols,
    })
}

/// Fetch every UTC day a crypto pair set still misses from Alpaca.
///
/// Like the equity backfill, pair-days are fetched concurrently within a
/// bounded request budget, fetchable days always complete, and per-day
/// failures are surfaced in the result instead of failing the whole range.
pub async fn run_local_crypto_backfill(
    options: LocalCryptoBackfillOptions,
) -> Result<CryptoBackfillResult, LocalIngestError> {
    let pairs = normalize_pairs(&options.pairs)?;
    let selected = normalize_data_types(options.data_types.as_deref(), true)?;
    if options.end_date < options.start_date {
        return Err(LocalIngestError::new(
            "start_date must be on or before end_date",
        ));
    }
    require_credentials(&options.settings, "backfill")?;
    options.layout.initialize()?;

    let catalog: Arc<JsonCatalogStore> = match options.catalog.clone() {
        Some(catalog) => catalog,
        None => Arc::new(JsonCatalogStore::open(options.layout.catalog_path())?),
    };
    let store: Arc<dyn BlobStore> = Arc::new(LocalFilesystemStore::new(options.layout.objects()));
    let committer = Arc::new(LocalBackfillCommitter::new(
        store,
        Arc::clone(&catalog),
        CATALOG_FLUSH_RECORDS,
    ));
    let provider = crypto_provider(options.provider, &options.settings)?;
    let sessions: Vec<CoverageSession> = crypto_sessions(options.start_date, options.end_date)?
        .iter()
        .map(|session| CoverageSession::from_exchange(session, options.now))
        .collect();

    let run: SessionRun = {
        let provider = Arc::clone(&provider);
        let committer = Arc::clone(&committer);
        let selected = selected.clone();
        let now = options.now;
        let clock: Arc<dyn Fn() -> DateTime + Send + Sync> = Arc::new(move || now);
        Arc::new(move |pair, session| {
            let provider = Arc::clone(&provider);
            let committer: Arc<dyn AtomicIngestCommitter> = committer.clone();
            let selected = selected.clone();
            let clock = Arc::clone(&clock);
            Box::pin(async move {
                let transaction = CryptoIngestTransaction::new(
                    provider,
                    Arc::new(StagingPartitionWriter::new()),
                    committer,
                )
                .with_clock(clock);
                let result = transaction.run(&pair, &session, Some(&selected)).await?;
                Ok(TransactionOutcome::from(result))
            })
        })
    };

    let output = execute_backfill(
        BackfillPlan {
            layout: options.layout,
            catalog,
            symbols: pairs,
            sessions,
            selected,
            start_date: options.start_date,
            end_date: options.end_date,
            committer,
            retry_settle_seconds: options.retry_settle_seconds,
            progress: options.progress,
        },
        run,
    )
    .await?;
    Ok(CryptoBackfillResult {
        asset_class: "crypto".to_string(),
        data_root: output.data_root,
        errors: output.errors,
        requested_end: output.requested_end,
        requested_start: output.requested_start,
        sessions: output.sessions,
        symbols: output.symbols,
    })
}

/// Inputs for one local bars backfill.
pub struct LocalBarsBackfillOptions {
    /// Local layout the backfill writes into.
    pub layout: LocalLayout,
    /// Equity or crypto; options are rejected.
    pub asset_class: AssetClass,
    /// Symbols (equities) or pairs (crypto), canonicalized and deduplicated.
    pub symbols: Vec<String>,
    /// The bars interval to produce.
    pub interval: BarInterval,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Environment overrides used for credential checks and provider config.
    pub environ: BTreeMap<String, String>,
    /// Injected current instant used for session clocks.
    pub now: DateTime,
    /// Loaded lake settings providing provider credentials.
    pub settings: LakeSettings,
    /// Injected bars provider; otherwise built from the provider settings.
    pub provider: Option<Arc<dyn BarsDataProvider>>,
    /// Settle delay before the in-run retry pass; two seconds when absent.
    pub retry_settle_seconds: Option<f64>,
    /// Shared catalog store; the layout's file is opened when absent.
    pub catalog: Option<Arc<JsonCatalogStore>>,
}

/// Produce every session-aligned bars partition a symbol set still misses.
///
/// Sessions with terminal coverage are skipped (the bars classification
/// already invalidates derived rows whose trade revision changed), the
/// provider is tried first, and the stored trades derive the remainder. One
/// transaction commits a whole symbol window atomically; transient provider
/// failures get at most one settle-delayed retry pass in the same run.
pub async fn run_local_bars_backfill(
    options: LocalBarsBackfillOptions,
) -> Result<BackfillResult, LocalIngestError> {
    if !matches!(options.asset_class, AssetClass::Equity | AssetClass::Crypto) {
        return Err(LocalIngestError::new(
            "bars backfill supports equity and crypto only",
        ));
    }
    let symbols = match options.asset_class {
        AssetClass::Equity => normalize_symbols(&options.symbols)?,
        AssetClass::Crypto => normalize_pairs(&options.symbols)?,
        _ => unreachable!("asset class validated above"),
    };
    if options.end_date < options.start_date {
        return Err(LocalIngestError::new(
            "start_date must be on or before end_date",
        ));
    }
    require_credentials(&options.settings, "backfill")?;
    options.layout.initialize()?;

    let catalog: Arc<JsonCatalogStore> = match options.catalog.clone() {
        Some(catalog) => catalog,
        None => Arc::new(JsonCatalogStore::open(options.layout.catalog_path())?),
    };
    let repository: Arc<dyn CatalogRepository> = catalog.clone();
    let store = Arc::new(LocalFilesystemStore::new(options.layout.objects()));
    let blob: Arc<dyn BlobStore> = store.clone();
    let readable: Arc<dyn ReadableBlobStore> = store;
    let committer = Arc::new(LocalIngestCommitter::new(blob, Arc::clone(&repository)));
    let reader: Arc<dyn lake_ingestion::TradePartitionReader> =
        Arc::new(CatalogTradeReader::new(Arc::clone(&repository), readable));
    let provider = bars_provider(options.provider, &options.settings)?;

    let mut entries: Vec<SessionIngestEntry> = Vec::new();
    let mut succeeded = 0usize;
    for symbol in &symbols {
        let report = calculate_coverage(
            catalog.as_ref(),
            None,
            options.asset_class,
            &[DataType::Bars],
            std::slice::from_ref(symbol),
            Some(options.interval.value()),
            options.start_date,
            options.end_date,
            options.now,
        )
        .map_err(|error| LocalIngestError::new(error.to_string()))?;
        let sessions: Vec<CoverageSession> = report
            .entries
            .iter()
            .filter(|entry| entry.is_actionable())
            .map(|entry| entry.session)
            .collect();
        if sessions.is_empty() {
            continue;
        }
        let transaction = BarsIngestTransaction::new(
            Arc::clone(&provider),
            Arc::clone(&reader),
            Arc::new(StagingPartitionWriter::new()),
            committer.clone(),
        );
        let result = run_bars_window(
            &transaction,
            options.asset_class,
            symbol,
            options.interval,
            &sessions,
            options.retry_settle_seconds,
        )
        .await?;
        for session in &sessions {
            let record = result
                .coverage
                .iter()
                .find(|record| record.session_date == session.session_date)
                .expect("every planned session publishes one bars coverage row");
            let entry = SessionIngestEntry {
                data_type_rows: BTreeMap::from([(
                    DataType::Bars.value().to_string(),
                    record.row_count,
                )]),
                error: (record.status == CoverageStatus::Error).then(|| {
                    record
                        .error_message
                        .clone()
                        .unwrap_or_else(|| "provider request failed".to_string())
                }),
                retryable: result.retryable && record.status == CoverageStatus::Error,
                session_date: session.session_date.isoformat(),
                statuses: vec![record.status.value().to_string()],
                symbol: symbol.clone(),
            };
            if entry.error.is_none() {
                succeeded += 1;
            }
            entries.push(entry);
        }
    }

    let failures: Vec<SessionIngestEntry> = entries
        .iter()
        .filter(|entry| entry.error.is_some())
        .cloned()
        .collect();
    if !failures.is_empty() && succeeded == 0 {
        let detail = failures[0].error.clone().unwrap_or_default();
        return Err(LocalIngestError::new(format!(
            "Alpaca bars backfill failed: {detail}"
        )));
    }
    Ok(BackfillResult {
        data_root: options.layout.root().to_string_lossy().into_owned(),
        errors: failures,
        requested_end: options.end_date.isoformat(),
        requested_start: options.start_date.isoformat(),
        sessions: entries,
        symbols,
    })
}

/// Run one bars window with at most one settle-delayed retry over failures.
async fn run_bars_window(
    transaction: &BarsIngestTransaction,
    asset_class: AssetClass,
    symbol: &str,
    interval: BarInterval,
    sessions: &[CoverageSession],
    retry_settle_seconds: Option<f64>,
) -> Result<BarsIngestResult, LocalIngestError> {
    let result = transaction
        .run(asset_class, symbol, interval, sessions)
        .await?;
    if !result.retryable {
        return Ok(result);
    }
    let failed: Vec<CoverageSession> = sessions
        .iter()
        .filter(|session| {
            result.coverage.iter().any(|record| {
                record.session_date == session.session_date
                    && record.status == CoverageStatus::Error
            })
        })
        .copied()
        .collect();
    if failed.is_empty() {
        return Ok(result);
    }
    let settle = retry_settle_seconds.unwrap_or(RETRY_SETTLE_SECONDS);
    if settle > 0.0 {
        tokio::time::sleep(Duration::from_secs_f64(settle)).await;
    }
    let retried = transaction
        .run(asset_class, symbol, interval, &failed)
        .await?;
    let mut coverage: BTreeMap<Date, CoverageRecord> = result
        .coverage
        .iter()
        .map(|record| (record.session_date, record.clone()))
        .collect();
    let mut objects = result.objects;
    for record in retried.coverage {
        coverage.insert(record.session_date, record);
    }
    objects.extend(retried.objects);
    Ok(BarsIngestResult {
        symbol: result.symbol,
        asset_class,
        interval,
        coverage: sessions
            .iter()
            .filter_map(|session| coverage.remove(&session.session_date))
            .collect(),
        objects,
        retryable: false,
    })
}

/// Run the planned sessions, retrying transiently-failed ones once in-run.
async fn execute_backfill(
    plan: BackfillPlan,
    run: SessionRun,
) -> Result<BackfillOutput, LocalIngestError> {
    let mut complete = terminal_keys(plan.catalog.as_ref(), plan.start_date, plan.end_date)?;
    let planned: Vec<(String, CoverageSession)> = plan
        .symbols
        .iter()
        .flat_map(|symbol| {
            plan.sessions
                .iter()
                .filter(|session| {
                    !session_is_terminal(&complete, symbol, session.session_date, &plan.selected)
                })
                .map(|session| (symbol.clone(), *session))
        })
        .collect();

    let mut succeeded = 0usize;
    let mut entries: Vec<SessionIngestEntry> = Vec::with_capacity(planned.len());
    let progress = plan.progress.clone();
    for ((symbol, session), result) in planned
        .iter()
        .zip(run_planned(&planned, &run, progress).await)
    {
        entries.push(record_outcome(
            result,
            symbol,
            session,
            &mut succeeded,
            &mut complete,
        ));
    }

    let retryable: Vec<usize> = entries
        .iter()
        .enumerate()
        .filter(|(_, entry)| entry.error.is_some() && entry.retryable)
        .map(|(index, _)| index)
        .collect();
    if !retryable.is_empty() {
        let settle = plan.retry_settle_seconds.unwrap_or(RETRY_SETTLE_SECONDS);
        if settle > 0.0 {
            tokio::time::sleep(Duration::from_secs_f64(settle)).await;
        }
        let retry_plan: Vec<(String, CoverageSession)> = retryable
            .iter()
            .map(|index| planned[*index].clone())
            .collect();
        for (index, ((symbol, session), result)) in retryable.iter().zip(
            retry_plan
                .iter()
                .zip(run_planned(&retry_plan, &run, None).await),
        ) {
            entries[*index] =
                record_outcome(result, symbol, session, &mut succeeded, &mut complete);
        }
    }
    plan.committer.flush()?;

    let failures: Vec<SessionIngestEntry> = entries
        .iter()
        .filter(|entry| entry.error.is_some())
        .cloned()
        .collect();
    if !failures.is_empty() && succeeded == 0 {
        let detail = failures[0].error.clone().unwrap_or_default();
        return Err(LocalIngestError::new(format!(
            "Alpaca backfill failed: {detail}"
        )));
    }
    Ok(BackfillOutput {
        data_root: plan.layout.root().to_string_lossy().into_owned(),
        errors: failures,
        requested_end: plan.end_date.isoformat(),
        requested_start: plan.start_date.isoformat(),
        sessions: entries,
        symbols: plan.symbols,
    })
}

/// Run every planned session concurrently within the bounded budget.
///
/// The joined order matches the planned order regardless of completion order,
/// so the backfill result stays deterministic. Each finished session reports
/// one incremental progress snapshot as it completes.
async fn run_planned(
    planned: &[(String, CoverageSession)],
    run: &SessionRun,
    progress: Option<BackfillProgressSink>,
) -> Vec<Result<TransactionOutcome, DataError>> {
    let semaphore = Arc::new(Semaphore::new(BACKFILL_CONCURRENCY));
    let completed = Arc::new(AtomicUsize::new(0));
    let total = planned.len();
    let futures = planned.iter().map(|(symbol, session)| {
        let run = Arc::clone(run);
        let semaphore = Arc::clone(&semaphore);
        let progress = progress.clone();
        let completed = Arc::clone(&completed);
        let symbol = symbol.clone();
        let session = *session;
        async move {
            let _permit = semaphore
                .acquire()
                .await
                .expect("the backfill semaphore is never closed");
            let result = run(symbol.clone(), session).await;
            if let Some(sink) = progress {
                let done = completed.fetch_add(1, Ordering::AcqRel) + 1;
                let (data_type_rows, error) = match &result {
                    Ok(outcome) => (
                        outcome
                            .coverage
                            .iter()
                            .map(|record| (record.data_type.value().to_string(), record.row_count))
                            .collect(),
                        None,
                    ),
                    Err(failure) => (BTreeMap::new(), Some(data_error_message(failure))),
                };
                sink(BackfillProgress {
                    sessions_total: total,
                    sessions_done: done,
                    symbol,
                    session_date: session.session_date.isoformat(),
                    data_type_rows,
                    error,
                });
            }
            result
        }
    });
    join_all(futures).await
}

/// Build one entry, surfacing error coverage as the session error.
fn record_outcome(
    result: Result<TransactionOutcome, DataError>,
    symbol: &str,
    session: &CoverageSession,
    succeeded: &mut usize,
    complete: &mut BTreeMap<(String, Date, DataType), CoverageStatus>,
) -> SessionIngestEntry {
    match result {
        Ok(outcome) => {
            let entry = outcome_entry(&outcome);
            if entry.error.is_none() {
                *succeeded += 1;
                for record in &outcome.coverage {
                    complete.insert(
                        (record.symbol.clone(), record.session_date, record.data_type),
                        record.status,
                    );
                }
            }
            entry
        }
        Err(error) => failed_entry(&error, symbol, session),
    }
}

/// Build one entry for a session whose transaction returned a result.
fn outcome_entry(outcome: &TransactionOutcome) -> SessionIngestEntry {
    let errors: Vec<String> = outcome
        .coverage
        .iter()
        .filter(|record| record.status == CoverageStatus::Error)
        .map(|record| {
            record
                .error_message
                .clone()
                .unwrap_or_else(|| "provider request failed".to_string())
        })
        .collect();
    SessionIngestEntry {
        data_type_rows: outcome
            .coverage
            .iter()
            .map(|record| (record.data_type.value().to_string(), record.row_count))
            .collect(),
        error: errors.first().cloned(),
        retryable: outcome.retryable,
        session_date: outcome.session_date.isoformat(),
        statuses: outcome
            .coverage
            .iter()
            .map(|record| record.status.value().to_string())
            .collect(),
        symbol: outcome.symbol.clone(),
    }
}

/// Build one entry for a session whose transaction failed outright.
///
/// The transaction deliberately propagates failed commits without a
/// compensating write, so the session is reported as failed without coverage
/// and the next ensure retries it.
fn failed_entry(error: &DataError, symbol: &str, session: &CoverageSession) -> SessionIngestEntry {
    SessionIngestEntry {
        data_type_rows: BTreeMap::new(),
        error: Some(data_error_message(error)),
        retryable: false,
        session_date: session.session_date.isoformat(),
        statuses: Vec::new(),
        symbol: symbol.to_string(),
    }
}

/// Format one data-layer failure the way Python's exception class naming does.
fn data_error_message(error: &DataError) -> String {
    let kind = match error {
        DataError::TypeError(_) => "TypeError",
        DataError::ValueError(_) => "ValueError",
        DataError::Lookahead(_) => "LookaheadError",
        DataError::Incomplete(_) => "IncompleteError",
        DataError::Unsupported(_) => "UnsupportedError",
        DataError::EnsureJob(_) => "EnsureJobError",
        DataError::Arithmetic(_) => "ArithmeticError",
        DataError::Protocol(_) => "ProtocolError",
        DataError::NotFound(_) => "NotFoundError",
        DataError::Conflict(_) => "ConflictError",
        DataError::Io(_) => "IoError",
    };
    let detail = error.to_string();
    let detail = detail.trim();
    if detail.is_empty() {
        kind.to_string()
    } else {
        format!("{kind}: {detail}")
    }
}

/// Canonicalize, validate, and deduplicate backfill symbols in order.
fn normalize_symbols(symbols: &[String]) -> Result<Vec<String>, LocalIngestError> {
    let mut normalized: Vec<String> = Vec::new();
    for value in symbols {
        let canonical = canonical_symbol(value)?;
        symbol_filename(&canonical)?;
        if !normalized.contains(&canonical) {
            normalized.push(canonical);
        }
    }
    if normalized.is_empty() {
        return Err(LocalIngestError::new(
            "backfill requires at least one symbol",
        ));
    }
    Ok(normalized)
}

/// Canonicalize, validate, and deduplicate crypto pairs in order.
fn normalize_pairs(pairs: &[String]) -> Result<Vec<String>, LocalIngestError> {
    let mut normalized: Vec<String> = Vec::new();
    for value in pairs {
        let canonical = value.trim().to_uppercase();
        if !canonical.contains('/') {
            return Err(LocalIngestError::new(format!(
                "invalid crypto pair: {value:?}"
            )));
        }
        symbol_filename(&canonical)?;
        if !normalized.contains(&canonical) {
            normalized.push(canonical);
        }
    }
    if normalized.is_empty() {
        return Err(LocalIngestError::new(
            "backfill requires at least one crypto pair",
        ));
    }
    Ok(normalized)
}

/// Return the requested data types in canonical trade-then-quote order.
fn normalize_data_types(
    values: Option<&[DataType]>,
    crypto: bool,
) -> Result<Vec<DataType>, LocalIngestError> {
    const CANONICAL: [DataType; 2] = [DataType::Trade, DataType::Quote];
    let message = if crypto {
        "crypto data_types must contain trades or quotes"
    } else {
        "data_types must contain trade or quote"
    };
    match values {
        None => Ok(CANONICAL.to_vec()),
        Some(values)
            if values.is_empty() || values.iter().any(|value| !CANONICAL.contains(value)) =>
        {
            Err(LocalIngestError::new(message))
        }
        Some(values) => Ok(CANONICAL
            .into_iter()
            .filter(|data_type| values.contains(data_type))
            .collect()),
    }
}

/// Index the catalog's terminal statuses for the requested window.
fn terminal_keys(
    catalog: &dyn CatalogRepository,
    start: Date,
    end: Date,
) -> Result<BTreeMap<(String, Date, DataType), CoverageStatus>, LocalIngestError> {
    let query = CoverageQuery {
        start: Some(start),
        end: Some(end),
        ..CoverageQuery::default()
    };
    Ok(catalog
        .list(&query)?
        .into_iter()
        .map(|record| {
            (
                (record.symbol, record.session_date, record.data_type),
                record.status,
            )
        })
        .collect())
}

/// Whether every requested data type is terminal for one session.
///
/// Only the requested datasets count: a trade-only backfill must not re-plan a
/// session merely because its quotes are still missing.
fn session_is_terminal(
    keys: &BTreeMap<(String, Date, DataType), CoverageStatus>,
    symbol: &str,
    session_date: Date,
    data_types: &[DataType],
) -> bool {
    data_types.iter().all(|data_type| {
        matches!(
            keys.get(&(symbol.to_string(), session_date, *data_type)),
            Some(CoverageStatus::Complete | CoverageStatus::Empty)
        )
    })
}

/// Select the session one ingest fetches.
fn select_session(
    session_date: Option<Date>,
    now: DateTime,
) -> Result<CoverageSession, LocalIngestError> {
    match session_date {
        None => {
            let start = now.date().add_days(-DEFAULT_LOOKBACK_DAYS);
            let sessions = xnys_sessions(start, now.date())?;
            sessions
                .iter()
                .map(|session| CoverageSession::from_exchange(session, now))
                .filter(|session| session.close_time <= now)
                .max_by_key(|session| session.session_date)
                .ok_or_else(|| LocalIngestError::new("no closed XNYS session is available"))
        }
        Some(date) => {
            let sessions = xnys_sessions(date, date)?;
            match sessions.first() {
                Some(session) if session.close_time <= now => {
                    Ok(CoverageSession::from_exchange(session, now))
                }
                _ => Err(LocalIngestError::new(
                    "session-date must identify a closed XNYS session",
                )),
            }
        }
    }
}

/// Reject a missing provider credential with the Python operation wording.
fn require_credentials(settings: &LakeSettings, operation: &str) -> Result<(), LocalIngestError> {
    if settings.provider.api_key.is_none() || settings.provider.api_secret.is_none() {
        return Err(LocalIngestError::new(format!(
            "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET are required \
             for local {operation}"
        )));
    }
    Ok(())
}

/// Build the equity provider, constructing the production client when absent.
fn equity_provider(
    provider: Option<Arc<dyn EquityDataProvider>>,
    settings: &LakeSettings,
) -> Result<Arc<dyn EquityDataProvider>, LocalIngestError> {
    match provider {
        Some(provider) => Ok(provider),
        None => {
            let client =
                AlpacaClient::with_reqwest(provider_config(settings), shared_governor(settings))
                    .map_err(|error| LocalIngestError::new(error.to_string()))?;
            Ok(Arc::new(client))
        }
    }
}

/// Build the crypto provider, constructing the production client when absent.
fn crypto_provider(
    provider: Option<Arc<dyn CryptoDataProvider>>,
    settings: &LakeSettings,
) -> Result<Arc<dyn CryptoDataProvider>, LocalIngestError> {
    match provider {
        Some(provider) => Ok(provider),
        None => {
            let client =
                AlpacaClient::with_reqwest(provider_config(settings), shared_governor(settings))
                    .map_err(|error| LocalIngestError::new(error.to_string()))?;
            Ok(Arc::new(client))
        }
    }
}

/// Build the bars provider, constructing the production client when absent.
fn bars_provider(
    provider: Option<Arc<dyn BarsDataProvider>>,
    settings: &LakeSettings,
) -> Result<Arc<dyn BarsDataProvider>, LocalIngestError> {
    match provider {
        Some(provider) => Ok(provider),
        None => {
            let client =
                AlpacaClient::with_reqwest(provider_config(settings), shared_governor(settings))
                    .map_err(|error| LocalIngestError::new(error.to_string()))?;
            Ok(Arc::new(client))
        }
    }
}

/// Map the loaded provider settings onto the client configuration.
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

/// The process-wide request-budget governor every production client shares.
fn shared_governor(settings: &LakeSettings) -> Arc<RequestGovernor> {
    static GOVERNOR: OnceLock<Arc<RequestGovernor>> = OnceLock::new();
    Arc::clone(
        GOVERNOR
            .get_or_init(|| RequestGovernor::new(settings.provider.request.requests_per_minute)),
    )
}
