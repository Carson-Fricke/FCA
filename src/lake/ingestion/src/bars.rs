//! Session-aligned OHLCV bars: provider-first with exact trade derivation.
//!
//! # The fixed bar convention
//!
//! Bars are session-aligned OHLCV with exact decimals end to end:
//!
//! * **Daily** (`1d`) buckets equal one exchange session, `[open, close]`
//!   inclusive for equities and `[open, close)` for crypto UTC days. The row timestamp is the session open in UTC, so an equity
//!   daily row is stamped 14:30Z (13:30Z on early closes) and a crypto daily
//!   row is stamped 00:00Z.
//! * **1-minute** (`1min`) buckets are session-open-aligned minute windows.
//!   Bucket `k` starts at `open + k * 60s`; a trade stamped exactly at the
//!   session close belongs to the final window, so the union of the windows
//!   covers the equity session inclusively with no orphaned closing print.
//!   Crypto midnight belongs to the following UTC day.
//!
//! XNYS sessions come from the lake session calendars and crypto sessions are
//! UTC calendar days, exactly as the coverage planner produces them. Provider
//! bars are first re-stamped onto these buckets (daily rows collapse to the
//! session open, minute rows floor to the aligned window start); bars outside
//! every requested session (for example SIP extended-hours bars) are excluded
//! by construction. Provider bars are start-stamped, so a provider session is
//! the half-open `[open, close)` window: the row stamped exactly at the close
//! starts the following extended-hours period and never belongs to the
//! session. Equity event prints keep the closed `[open, close]` window;
//! crypto event prints belong to half-open UTC days.
//! Derived bars aggregate the stored trades in the canonical
//! trade sort order, so the OHLCV is deterministic and independent of trade
//! arrival order: open is the first trade price, high/low are the running
//! extremes, close is the last trade price, and volume is the exact decimal
//! sum of trade sizes.
//!
//! # Provider-first flow
//!
//! One run covers one symbol, one interval, and a planned session list. The
//! transaction fetches the union window from the provider once through the
//! existing governor/lane/retry path. Sessions the provider did not cover
//! (empty or permanently unsupported old ranges) are derived from the stored
//! trade partitions for those sessions; a session whose trades cannot be read
//! publishes error coverage. Empty sessions still write a valid empty Parquet
//! object with terminal `empty` coverage. Acquisition is batched over the
//! window; each session publishes through one atomic symbol-day commit.

use std::sync::Arc;

use crate::trade_inputs::{crypto_trades, equity_trades, CryptoTradeRecord, EquityTradeRecord};
use futures_util::future::BoxFuture;
use harness_market_reader::{
    MarketDataReader, MarketDataSnapshot, Partition, PartitionSource, ReaderError, SnapshotRequest,
};
use lake_catalog::{BarSource, CatalogRepository, CoverageRecord, CoverageStatus};
use lake_contracts::decimal::ONE_UNSCALED;
use lake_contracts::{AssetClass, BarInterval, DataError, DataType, Date, DateTime, Decimal18};
use lake_coverage::CoverageSession;
use lake_providers::{NormalizedBar, NormalizedCryptoTrade, NormalizedTrade, ProviderError};
use lake_storage::{ParquetMetadata, ReadableBlobStore, StorageCapGuard};

use crate::common::{
    canonical_symbol, data_error_message, fetch_with_transient_retry, provider_error_message,
    AtomicIngestCommitter, PartitionWriter, TransientRetry,
};
use crate::crypto::compare_trades as compare_crypto_trades;
use crate::equities::compare_trades as compare_equity_trades;
use crate::providers::BarsDataProvider;

/// Published metadata for one bars run over a planned session window.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BarsIngestResult {
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// Asset class the run covered.
    pub asset_class: AssetClass,
    /// Interval the run produced.
    pub interval: BarInterval,
    /// One coverage row per planned session, in session order.
    pub coverage: Vec<CoverageRecord>,
    /// One object metadata entry per written partition, in write order.
    pub objects: Vec<ParquetMetadata>,
    /// Whether a provider failure made the run partially retryable in-ensure.
    pub retryable: bool,
}

/// One stored trade restored from a pinned trade partition.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StoredTrade {
    /// One equity trade.
    Equity(EquityTradeRecord),
    /// One crypto trade.
    Crypto(CryptoTradeRecord),
}

/// Stored trades for one session plus the source partition revision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredTradePartition {
    /// Typed records in stored row order.
    pub records: Vec<StoredTrade>,
    /// The trade partition revision the bars derivation is keyed to.
    pub revision: String,
}

/// Boundary for reading one session's stored trade partition.
pub trait TradePartitionReader: Send + Sync {
    /// Return the stored trades for one symbol session, or `None` when no
    /// trade partition exists for it.
    fn read<'a>(
        &'a self,
        asset_class: AssetClass,
        symbol: &'a str,
        session_date: Date,
    ) -> BoxFuture<'a, Result<Option<StoredTradePartition>, DataError>>;
}

/// Catalog-backed trade reader over the lake's readable blob boundary.
pub struct CatalogTradeReader {
    catalog: Arc<dyn CatalogRepository>,
    store: Arc<dyn ReadableBlobStore>,
}

impl CatalogTradeReader {
    /// Build a reader that resolves the trade object through the catalog.
    pub fn new(catalog: Arc<dyn CatalogRepository>, store: Arc<dyn ReadableBlobStore>) -> Self {
        Self { catalog, store }
    }
}

impl TradePartitionReader for CatalogTradeReader {
    fn read<'a>(
        &'a self,
        asset_class: AssetClass,
        symbol: &'a str,
        session_date: Date,
    ) -> BoxFuture<'a, Result<Option<StoredTradePartition>, DataError>> {
        Box::pin(async move {
            let Some(record) =
                self.catalog
                    .get(asset_class, DataType::Trade, None, symbol, session_date)?
            else {
                return Ok(None);
            };
            let Some(path) = record.object_path else {
                return Ok(None);
            };
            let checksum = record.checksum.ok_or_else(|| {
                DataError::value("trade partition metadata is outdated: checksum required")
            })?;
            let snapshot = MarketDataSnapshot::new(
                SnapshotRequest {
                    asset_class: asset_class.value().into(),
                    data_type: "trade".into(),
                    symbol: symbol.into(),
                    start_date: session_date.isoformat(),
                    end_date: session_date.isoformat(),
                    interval: None,
                },
                vec![Partition {
                    session_date: session_date.isoformat(),
                    object_path: path,
                    checksum: checksum.clone(),
                    row_count: record.row_count,
                    byte_size: record.byte_size,
                }],
            )
            .map_err(DataError::io)?;
            let reader = MarketDataReader::configured().map_err(DataError::io)?;
            let mut records = Vec::new();
            reader
                .visit(&snapshot, &TradeObjects(self.store.clone()), |batch| {
                    match asset_class {
                        AssetClass::Equity => records.extend(
                            equity_trades(&[batch])
                                .map_err(|e| ReaderError::Decode(e.to_string()))?
                                .into_iter()
                                .map(StoredTrade::Equity),
                        ),
                        AssetClass::Crypto => records.extend(
                            crypto_trades(&[batch])
                                .map_err(|e| ReaderError::Decode(e.to_string()))?
                                .into_iter()
                                .map(StoredTrade::Crypto),
                        ),
                        _ => {
                            return Err(ReaderError::Decode(
                                "bars derivation supports equity and crypto only".into(),
                            ))
                        }
                    }
                    Ok(())
                })
                .await
                .map_err(DataError::io)?;
            Ok(Some(StoredTradePartition {
                records,
                revision: checksum,
            }))
        })
    }
}

struct TradeObjects(Arc<dyn ReadableBlobStore>);
impl PartitionSource for TradeObjects {
    fn read<'a>(
        &'a self,
        partition: &'a Partition,
    ) -> BoxFuture<'a, Result<bytes::Bytes, ReaderError>> {
        let store = self.0.clone();
        let path = partition.object_path.clone();
        Box::pin(async move {
            tokio::task::spawn_blocking(move || {
                store
                    .read(&path)
                    .map(bytes::Bytes::from)
                    .map_err(|e| ReaderError::Source(e.to_string()))
            })
            .await
            .map_err(|e| ReaderError::Source(e.to_string()))?
        })
    }
}

/// Fetch, derive, stage, and atomically publish one bars window.
#[derive(Clone)]
pub struct BarsIngestTransaction {
    provider: Arc<dyn BarsDataProvider>,
    reader: Arc<dyn TradePartitionReader>,
    writer: Arc<dyn PartitionWriter>,
    committer: Arc<dyn AtomicIngestCommitter>,
    cap_guard: Option<Arc<StorageCapGuard>>,
    retry: TransientRetry,
}

impl BarsIngestTransaction {
    /// Build a transaction over injected provider, trade reader, writer, and
    /// committer.
    pub fn new(
        provider: Arc<dyn BarsDataProvider>,
        reader: Arc<dyn TradePartitionReader>,
        writer: Arc<dyn PartitionWriter>,
        committer: Arc<dyn AtomicIngestCommitter>,
    ) -> Self {
        Self {
            provider,
            reader,
            writer,
            committer,
            cap_guard: None,
            retry: TransientRetry::default(),
        }
    }

    /// Attach a storage capacity guard evaluated before fetch and commit.
    pub fn with_cap_guard(mut self, guard: Arc<StorageCapGuard>) -> Self {
        self.cap_guard = Some(guard);
        self
    }

    /// Override the transient retry budget.
    pub fn with_retry(mut self, retry: TransientRetry) -> Self {
        self.retry = retry;
        self
    }

    /// Fetch and publish every planned session for one symbol and interval.
    ///
    /// Provider bars are used wherever the provider returns them; a session
    /// with no provider bars falls back to stored-trade derivation after the
    /// caller has ensured trades for the window. Per-session failures surface
    /// as error coverage. Each session's object and coverage publish atomically;
    /// a persistence failure propagates without replaying earlier publications.
    pub async fn run(
        &self,
        asset_class: AssetClass,
        symbol: &str,
        interval: BarInterval,
        sessions: &[CoverageSession],
    ) -> Result<BarsIngestResult, DataError> {
        let session_memory = lake_pipeline::Pipeline::global().session().await?;
        if !matches!(
            asset_class,
            AssetClass::Equity | AssetClass::Crypto | AssetClass::Option
        ) {
            return Err(DataError::value(format!(
                "bars ingest supports equity and crypto only, not {asset_class}"
            )));
        }
        let symbol = canonical_symbol(symbol)?;
        if let Some(guard) = &self.cap_guard {
            guard.enforce_ingest(0)?;
        }
        let mut objects = Vec::new();
        let mut coverage = Vec::new();
        let mut provider_failure: Option<ProviderError> = None;

        if !sessions.is_empty() {
            let window_start = sessions
                .iter()
                .map(|session| session.open_time)
                .min()
                .expect("a nonempty session list has an open");
            let window_end = sessions
                .iter()
                .map(|session| session.close_time)
                .max()
                .expect("a nonempty session list has a close");
            let provider_bars = match fetch_with_transient_retry(self.retry, || {
                self.provider.fetch_bars(
                    asset_class,
                    symbol.as_str(),
                    interval,
                    window_start,
                    window_end,
                )
            })
            .await
            {
                Ok(bars) => bars,
                Err(error) => {
                    provider_failure = Some(error);
                    Vec::new()
                }
            };
            let mut grouped: Vec<Vec<NormalizedBar>> = vec![Vec::new(); sessions.len()];
            for bar in provider_bars {
                if let Some(index) = session_index(sessions, bar.timestamp) {
                    grouped[index].push(bar);
                }
            }
            for (session, bars) in sessions.iter().zip(grouped) {
                if !bars.is_empty()
                    || (asset_class == AssetClass::Option && provider_failure.is_none())
                {
                    let transaction = self.clone();
                    let partition_symbol = symbol.clone();
                    let partition_session = *session;
                    let staged = crate::common::blocking_stage(move || {
                        transaction.stage_provider(
                            asset_class,
                            &partition_symbol,
                            interval,
                            &partition_session,
                            &bars,
                        )
                    })
                    .await;
                    match staged {
                        Ok((metadata, rows, revision)) => {
                            coverage.push(bar_coverage(
                                asset_class,
                                &symbol,
                                interval,
                                session,
                                status_for(session.is_partial, rows),
                                &metadata,
                                rows,
                                BarSource::Provider,
                                &revision,
                            )?);
                            objects.push(metadata);
                        }
                        Err(error) => coverage.push(error_coverage(
                            asset_class,
                            &symbol,
                            interval,
                            session,
                            data_error_message(&error),
                        )?),
                    }
                    continue;
                }
                match self
                    .stage_derived(asset_class, &symbol, interval, session)
                    .await
                {
                    Ok(Some((metadata, rows, revision))) => {
                        coverage.push(bar_coverage(
                            asset_class,
                            &symbol,
                            interval,
                            session,
                            status_for(session.is_partial, rows),
                            &metadata,
                            rows,
                            BarSource::Derived,
                            &revision,
                        )?);
                        objects.push(metadata);
                    }
                    Ok(None) => {
                        let message = provider_failure
                            .as_ref()
                            .map(provider_error_message)
                            .unwrap_or_else(|| {
                                "trades are not available for bars derivation".to_string()
                            });
                        coverage.push(error_coverage(
                            asset_class,
                            &symbol,
                            interval,
                            session,
                            message,
                        )?);
                    }
                    Err(error) => coverage.push(error_coverage(
                        asset_class,
                        &symbol,
                        interval,
                        session,
                        data_error_message(&error),
                    )?),
                }
            }
        }

        let staged = self.writer.drain_staged();
        let retryable = provider_failure
            .as_ref()
            .is_some_and(ProviderError::is_transient)
            && coverage
                .iter()
                .any(|record| record.status == CoverageStatus::Error);
        let result = BarsIngestResult {
            symbol,
            asset_class,
            interval,
            coverage,
            objects,
            retryable,
        };
        let transaction = self.clone();
        crate::common::blocking_stage(move || {
            let _session_memory = session_memory;
            transaction.finish(staged, result)
        })
        .await
    }

    /// Publish without blocking concurrent provider I/O on the async runtime.
    fn finish(
        &self,
        staged: Vec<crate::StagedParquetObject>,
        result: BarsIngestResult,
    ) -> Result<BarsIngestResult, DataError> {
        if let Some(guard) = &self.cap_guard {
            let additional_bytes = staged
                .iter()
                .map(|object| object.metadata.bytes)
                .sum::<u64>();
            if let Err(error) = guard.enforce_ingest(additional_bytes) {
                self.writer.reset_staged();
                return Err(error);
            }
        }
        // A range fetch can span many sessions, but the publication/lease unit
        // is one symbol-day. Validate the full mapping before publishing any
        // day, then move its payload into its one commit without cloning bytes.
        let mut objects = std::collections::BTreeMap::new();
        for object in staged {
            if objects
                .insert(object.metadata.path.clone(), object)
                .is_some()
            {
                return Err(DataError::value("bars staged a duplicate partition"));
            }
        }
        let mut dates = std::collections::BTreeSet::new();
        for record in &result.coverage {
            if !dates.insert(record.session_date) {
                return Err(DataError::value("bars coverage repeats a session"));
            }
            if let Some(path) = &record.object_path {
                if !objects.contains_key(path) {
                    return Err(DataError::value("bars coverage has no staged object"));
                }
            }
        }
        if objects.len()
            != result
                .coverage
                .iter()
                .filter(|record| record.object_path.is_some())
                .count()
        {
            return Err(DataError::value("bars staged an unreferenced object"));
        }
        let outcome = result.coverage.iter().try_for_each(|record| {
            let object: Vec<_> = record
                .object_path
                .as_ref()
                .and_then(|path| objects.remove(path))
                .into_iter()
                .collect();
            self.committer.commit(&object, std::slice::from_ref(record))
        });
        self.writer.reset_staged();
        outcome?;
        Ok(result)
    }

    /// Stage one provider-served session, re-stamped onto the fixed buckets.
    fn stage_provider(
        &self,
        asset_class: AssetClass,
        symbol: &str,
        interval: BarInterval,
        session: &CoverageSession,
        bars: &[NormalizedBar],
    ) -> Result<(ParquetMetadata, u64, String), DataError> {
        let pipeline = lake_pipeline::Pipeline::global();
        let _cpu = pipeline.cpu_blocking()?;
        let mut validation = pipeline.timer(lake_pipeline::Stage::Validate);
        validation.rows(bars.len());
        let bars = provider_bar_records(symbol, bars, interval, session)?;
        drop(validation);
        let batch = crate::columnar::bars(&bars, asset_class, symbol)?;
        let metadata = self.writer.write(
            &batch,
            session.session_date,
            asset_class,
            DataType::Bars,
            Some(interval.value()),
            symbol,
        )?;
        let revision = metadata.checksum.clone();
        Ok((metadata, bars.len() as u64, revision))
    }

    /// Stage one derived session, or report that its trades are unavailable.
    async fn stage_derived(
        &self,
        asset_class: AssetClass,
        symbol: &str,
        interval: BarInterval,
        session: &CoverageSession,
    ) -> Result<Option<(ParquetMetadata, u64, String)>, DataError> {
        // An empty option bars response is a valid empty session. Options do
        // not borrow stock trade schemas or fetch an unrequested trade feed.
        if asset_class == AssetClass::Option {
            return Ok(None);
        }
        let Some(partition) = self
            .reader
            .read(asset_class, symbol, session.session_date)
            .await?
        else {
            return Ok(None);
        };
        let transaction = self.clone();
        let symbol = symbol.to_string();
        let session = *session;
        crate::common::blocking_stage(move || {
            transaction.stage_trade_partition(asset_class, &symbol, interval, &session, partition)
        })
        .await
        .map(Some)
    }

    /// Derive and encode stored ticks within the shared CPU-stage budget.
    fn stage_trade_partition(
        &self,
        asset_class: AssetClass,
        symbol: &str,
        interval: BarInterval,
        session: &CoverageSession,
        partition: StoredTradePartition,
    ) -> Result<(ParquetMetadata, u64, String), DataError> {
        let pipeline = lake_pipeline::Pipeline::global();
        let _cpu = pipeline.cpu_blocking()?;
        let derive = pipeline.timer(lake_pipeline::Stage::Derive);
        let bars = match asset_class {
            AssetClass::Equity => {
                let records = equity_records(partition.records)?;
                derive_equity_bars(symbol, &records, interval, session)?
            }
            AssetClass::Crypto => {
                let records = crypto_records(partition.records)?;
                derive_crypto_bars(symbol, &records, interval, session)?
            }
            other => {
                return Err(DataError::value(format!(
                    "bars derivation supports equity and crypto only, not {other}"
                )))
            }
        };
        drop(derive);
        let batch = crate::columnar::bars(&bars, asset_class, symbol)?;
        let metadata = self.writer.write(
            &batch,
            session.session_date,
            asset_class,
            DataType::Bars,
            Some(interval.value()),
            symbol,
        )?;
        Ok((metadata, bars.len() as u64, partition.revision))
    }
}

/// Derive equity session bars from stored trades.
///
/// Trades are ordered on the pinned canonical trade sort before aggregation,
/// so the result is independent of stored (and provider arrival) order.
pub fn derive_equity_bars(
    symbol: &str,
    trades: &[EquityTradeRecord],
    interval: BarInterval,
    session: &CoverageSession,
) -> Result<Vec<NormalizedBar>, DataError> {
    let mut normalized: Vec<NormalizedTrade> = trades.iter().map(normalize_equity).collect();
    normalized.sort_by(compare_equity_trades);
    let points: Vec<TradePoint> = normalized
        .iter()
        .map(|trade| TradePoint {
            timestamp: trade.timestamp,
            price: trade.price,
            size: Decimal18::from_unscaled(i128::from(trade.size) * ONE_UNSCALED)
                .expect("a u32 share count fits decimal128(38, 18)"),
        })
        .collect();
    aggregate_bars(symbol, &points, interval, session)
}

/// Derive crypto UTC-day bars, excluding the following day's midnight.
pub fn derive_crypto_bars(
    symbol: &str,
    trades: &[CryptoTradeRecord],
    interval: BarInterval,
    session: &CoverageSession,
) -> Result<Vec<NormalizedBar>, DataError> {
    let mut normalized: Vec<NormalizedCryptoTrade> = trades.iter().map(normalize_crypto).collect();
    normalized.sort_by(compare_crypto_trades);
    let points: Vec<TradePoint> = normalized
        .iter()
        // Crypto owns [UTC midnight, next midnight); equity retains its
        // inclusive closing auction convention in the shared aggregator.
        .filter(|trade| trade.timestamp != session.close_time)
        .map(|trade| TradePoint {
            timestamp: trade.timestamp,
            price: trade.price,
            size: trade.size,
        })
        .collect();
    aggregate_bars(symbol, &points, interval, session)
}

/// One exact trade point ready for aggregation.
struct TradePoint {
    timestamp: DateTime,
    price: Decimal18,
    size: Decimal18,
}

/// Aggregate canonically ordered trade points into session-aligned bars.
fn aggregate_bars(
    symbol: &str,
    points: &[TradePoint],
    interval: BarInterval,
    session: &CoverageSession,
) -> Result<Vec<NormalizedBar>, DataError> {
    let mut bars: Vec<NormalizedBar> = Vec::new();
    for point in points {
        if point.timestamp < session.open_time || point.timestamp > session.close_time {
            continue;
        }
        let bucket = bucket_start(point.timestamp, interval, session);
        match bars.last_mut() {
            Some(active) if active.timestamp == bucket => {
                active.high = active.high.max(point.price);
                active.low = active.low.min(point.price);
                active.close = point.price;
                active.volume = add_volume(active.volume, point.size)?;
            }
            _ => bars.push(NormalizedBar {
                timestamp: bucket,
                symbol: symbol.to_string(),
                open: point.price,
                high: point.price,
                low: point.price,
                close: point.price,
                volume: point.size,
            }),
        }
    }
    Ok(bars)
}

/// Re-stamp provider bars onto the fixed session buckets.
///
/// Daily provider bars collapse to the session open; minute bars floor to the
/// open-aligned window start (a bar exactly at the close lands in the final
/// window). Two provider bars landing on the same bucket are a provider
/// protocol failure for that session.
fn provider_bar_records(
    symbol: &str,
    bars: &[NormalizedBar],
    interval: BarInterval,
    session: &CoverageSession,
) -> Result<Vec<NormalizedBar>, DataError> {
    let mut normalized: Vec<NormalizedBar> = bars
        .iter()
        .map(|bar| NormalizedBar {
            timestamp: bucket_start(bar.timestamp, interval, session),
            symbol: symbol.to_string(),
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
            volume: bar.volume,
        })
        .collect();
    normalized.sort_by_key(|bar| bar.timestamp);
    for pair in normalized.windows(2) {
        if pair[0].timestamp == pair[1].timestamp {
            return Err(DataError::value(format!(
                "provider bars overlap a {} bucket on {}",
                interval.value(),
                session.session_date
            )));
        }
    }
    Ok(normalized)
}

/// The session-open-aligned bucket start one timestamp belongs to.
///
/// Provider rows stamped at or after the close never reach this function
/// (`session_index` groups provider bars on the half-open session window); the
/// close-instant clamp serves event prints, so a trade exactly at the close
/// lands in the final window.
fn bucket_start(timestamp: DateTime, interval: BarInterval, session: &CoverageSession) -> DateTime {
    match interval {
        BarInterval::OneDay => session.open_time,
        BarInterval::OneMinute => {
            let elapsed = (timestamp.micros() - session.open_time.micros()).max(0);
            let last = minute_window_count(session) - 1;
            let index = (elapsed / 60_000_000).min(last);
            DateTime::from_micros(session.open_time.micros() + index * 60_000_000)
        }
    }
}

/// Number of open-aligned minute windows covering `[open, close]` inclusively.
fn minute_window_count(session: &CoverageSession) -> i64 {
    let total = session.close_time.micros() - session.open_time.micros();
    ((total + 59_999_999) / 60_000_000).max(1)
}

/// The index of the session containing one start-stamped provider bar, if any.
///
/// Provider bars are stamped at the start of the interval they cover, so the
/// session window is half-open `[open, close)`: the row stamped exactly at the
/// close is the first extended-hours bar of the following period, not part of
/// this session. Event prints use the closed `[open, close]` window instead.
fn session_index(sessions: &[CoverageSession], timestamp: DateTime) -> Option<usize> {
    sessions
        .iter()
        .position(|session| session.open_time <= timestamp && timestamp < session.close_time)
}

/// The coverage status for one written bars partition.
fn status_for(session_partial: bool, rows: u64) -> CoverageStatus {
    if session_partial {
        CoverageStatus::Partial
    } else if rows > 0 {
        CoverageStatus::Complete
    } else {
        CoverageStatus::Empty
    }
}

/// Build the coverage row for one written bars partition.
#[allow(clippy::too_many_arguments)]
fn bar_coverage(
    asset_class: AssetClass,
    symbol: &str,
    interval: BarInterval,
    session: &CoverageSession,
    status: CoverageStatus,
    metadata: &ParquetMetadata,
    rows: u64,
    source: BarSource,
    revision: &str,
) -> Result<CoverageRecord, DataError> {
    CoverageRecord::new(
        asset_class,
        DataType::Bars,
        symbol,
        session.session_date,
        status,
        Some(metadata.path.clone()),
        rows,
        metadata.bytes,
        Some(metadata.checksum.clone()),
        None,
        Some(interval.value().to_string()),
    )?
    .with_bar_provenance(source, revision)
}

/// Build the error coverage row for one failed session.
fn error_coverage(
    asset_class: AssetClass,
    symbol: &str,
    interval: BarInterval,
    session: &CoverageSession,
    message: String,
) -> Result<CoverageRecord, DataError> {
    CoverageRecord::new(
        asset_class,
        DataType::Bars,
        symbol,
        session.session_date,
        CoverageStatus::Error,
        None,
        0,
        0,
        None,
        Some(message),
        Some(interval.value().to_string()),
    )
}

/// Sum two exact volumes, rejecting decimal overflow.
fn add_volume(left: Decimal18, right: Decimal18) -> Result<Decimal18, DataError> {
    Decimal18::from_unscaled(left.unscaled() + right.unscaled()).map_err(DataError::from)
}

/// Split a mixed stored-partition record list into equity records.
fn equity_records(records: Vec<StoredTrade>) -> Result<Vec<EquityTradeRecord>, DataError> {
    records
        .into_iter()
        .map(|record| match record {
            StoredTrade::Equity(record) => Ok(record),
            StoredTrade::Crypto(_) => Err(DataError::value(
                "equity bars derivation received a crypto trade partition",
            )),
        })
        .collect()
}

/// Split a mixed stored-partition record list into crypto records.
fn crypto_records(records: Vec<StoredTrade>) -> Result<Vec<CryptoTradeRecord>, DataError> {
    records
        .into_iter()
        .map(|record| match record {
            StoredTrade::Crypto(record) => Ok(record),
            StoredTrade::Equity(_) => Err(DataError::value(
                "crypto bars derivation received an equity trade partition",
            )),
        })
        .collect()
}

/// Restore one stored equity record as the normalized canonical shape used by
/// the pinned trade ordering.
fn normalize_equity(record: &EquityTradeRecord) -> NormalizedTrade {
    NormalizedTrade {
        timestamp: record.timestamp,
        symbol: record.symbol.clone(),
        trade_id: record.trade_id,
        exchange: record.exchange.clone(),
        price: record.price,
        size: record.size,
        conditions: record.conditions.clone(),
        tape: record.tape.clone(),
        update: record.update.clone(),
    }
}

/// Restore one stored crypto record as the normalized canonical shape used by
/// the pinned trade ordering.
fn normalize_crypto(record: &CryptoTradeRecord) -> NormalizedCryptoTrade {
    NormalizedCryptoTrade {
        timestamp: record.timestamp,
        symbol: record.symbol.clone(),
        trade_id: record.trade_id,
        exchange: record.exchange.clone(),
        price: record.price,
        size: record.size,
        taker_side: record.taker_side.clone(),
    }
}
