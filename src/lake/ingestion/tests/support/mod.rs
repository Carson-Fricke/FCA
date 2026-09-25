#![allow(dead_code)]

//! Shared fakes and fixtures for the ingestion integration tests.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use futures_util::future::BoxFuture;
use lake_catalog::{CatalogRepository, CoverageRecord, MemoryCatalogRepository};
use lake_contracts::{partition_path, AssetClass, DataError, DataType, Date, DateTime, Decimal18};
use lake_coverage::CoverageSession;
use lake_ingestion::{
    AtomicIngestCommitter, CryptoClock, CryptoDataProvider, EquityDataProvider, PartitionWriter,
    StagedParquetObject, TransientRetry,
};
use lake_providers::{
    NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedQuote, NormalizedTrade, ProviderError,
};
use lake_storage::{
    BlobStore, LocalFilesystemStore, ParquetMetadata, ReadableBlobStore, StorageCapGuard,
    StorageCapPolicy, StorageUsageProvider,
};
use serde_json::Value;

/// Lock a test mutex, ignoring poisoning.
pub fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Build a calendar date.
pub fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

/// Build a UTC instant on 2024-01-19.
pub fn instant(hour: u32, minute: u32, second: u32, micros: u32) -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 19, hour, minute, second, micros).expect("valid instant")
}

/// The 2024-01-19 equity session: 14:30 through 21:00 UTC.
pub fn equity_session(partial: bool) -> CoverageSession {
    CoverageSession {
        session_date: day(2024, 1, 19),
        open_time: instant(14, 30, 0, 0),
        close_time: instant(21, 0, 0, 0),
        is_partial: partial,
    }
}

/// One exact UTC calendar day.
pub fn crypto_day(session_date: Date, partial: bool) -> CoverageSession {
    CoverageSession {
        session_date,
        open_time: DateTime::midnight(session_date),
        close_time: DateTime::midnight(session_date.add_days(1)),
        is_partial: partial,
    }
}

/// An equity trade with the test defaults.
pub fn equity_trade(timestamp: DateTime, trade_id: u64, symbol: &str) -> NormalizedTrade {
    NormalizedTrade {
        timestamp,
        symbol: symbol.to_string(),
        trade_id,
        exchange: "XNAS".to_string(),
        price: Decimal18::parse("190.125").expect("price"),
        size: 100,
        conditions: vec!["@".to_string()],
        tape: "C".to_string(),
        update: None,
    }
}

/// An equity quote with the test defaults.
pub fn equity_quote(timestamp: DateTime, symbol: &str) -> NormalizedQuote {
    NormalizedQuote {
        timestamp,
        symbol: symbol.to_string(),
        bid_exchange: "XNAS".to_string(),
        bid_price: Decimal18::parse("190.12").expect("bid"),
        bid_size: 100,
        ask_exchange: "XNAS".to_string(),
        ask_price: Decimal18::parse("190.13").expect("ask"),
        ask_size: 200,
        conditions: Vec::new(),
        tape: "C".to_string(),
    }
}

/// A crypto trade with the test defaults.
pub fn crypto_trade(
    timestamp: DateTime,
    trade_id: Option<i64>,
    symbol: &str,
) -> NormalizedCryptoTrade {
    NormalizedCryptoTrade {
        timestamp,
        symbol: symbol.to_string(),
        trade_id,
        exchange: Some("CBSE".to_string()),
        price: Decimal18::parse("41234.123456789012345678").expect("price"),
        size: Decimal18::parse("0.01").expect("size"),
        taker_side: Some("B".to_string()),
    }
}

/// A crypto quote with the test defaults.
pub fn crypto_quote(timestamp: DateTime, symbol: &str) -> NormalizedCryptoQuote {
    NormalizedCryptoQuote {
        timestamp,
        symbol: symbol.to_string(),
        bid_exchange: Some("CBSE".to_string()),
        bid_price: Decimal18::parse("41234.12").expect("bid"),
        bid_size: Decimal18::parse("0.1").expect("bid size"),
        ask_exchange: Some("CBSE".to_string()),
        ask_price: Decimal18::parse("41235.12").expect("ask"),
        ask_size: Decimal18::parse("0.2").expect("ask size"),
    }
}

/// One recorded provider request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProviderCall {
    /// Requested data type.
    pub data_type: DataType,
    /// Requested symbols or pairs.
    pub symbols: Vec<String>,
    /// Inclusive fetch start.
    pub start: DateTime,
    /// Inclusive fetch end.
    pub end: DateTime,
}

/// Deterministic test fake for the equity provider boundary.
pub struct FakeEquityProvider {
    pub trades: Mutex<Vec<NormalizedTrade>>,
    pub quotes: Mutex<Vec<NormalizedQuote>>,
    pub trade_error: Mutex<Option<ProviderError>>,
    pub quote_error: Mutex<Option<ProviderError>>,
    trade_failures: AtomicU32,
    quote_failures: AtomicU32,
    pub calls: Mutex<Vec<ProviderCall>>,
}

impl FakeEquityProvider {
    /// Build a fake returning the supplied records.
    pub fn new(trades: Vec<NormalizedTrade>, quotes: Vec<NormalizedQuote>) -> Self {
        Self {
            trades: Mutex::new(trades),
            quotes: Mutex::new(quotes),
            trade_error: Mutex::new(None),
            quote_error: Mutex::new(None),
            trade_failures: AtomicU32::new(0),
            quote_failures: AtomicU32::new(0),
            calls: Mutex::new(Vec::new()),
        }
    }

    /// Make the next `failures` trade fetches fail transiently.
    pub fn failing_trades(self, failures: u32) -> Self {
        self.trade_failures.store(failures, Ordering::SeqCst);
        self
    }

    /// Make every trade fetch fail permanently.
    pub fn with_trade_error(self, error: ProviderError) -> Self {
        *guard(&self.trade_error) = Some(error);
        self
    }

    /// Make every quote fetch fail permanently.
    pub fn with_quote_error(self, error: ProviderError) -> Self {
        *guard(&self.quote_error) = Some(error);
        self
    }

    /// The recorded provider calls.
    pub fn calls(&self) -> Vec<ProviderCall> {
        guard(&self.calls).clone()
    }

    /// The recorded call data types in request order.
    pub fn call_kinds(&self) -> Vec<DataType> {
        guard(&self.calls)
            .iter()
            .map(|call| call.data_type)
            .collect()
    }

    fn record(&self, data_type: DataType, symbols: &[String], start: DateTime, end: DateTime) {
        guard(&self.calls).push(ProviderCall {
            data_type,
            symbols: symbols.to_vec(),
            start,
            end,
        });
    }
}

impl EquityDataProvider for FakeEquityProvider {
    fn fetch_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedTrade>, ProviderError>> {
        Box::pin(async move {
            self.record(DataType::Trade, symbols, start, end);
            if self.trade_failures.load(Ordering::SeqCst) > 0 {
                self.trade_failures.fetch_sub(1, Ordering::SeqCst);
                return Err(ProviderError::Transport(
                    "temporary DNS failure".to_string(),
                ));
            }
            if let Some(error) = guard(&self.trade_error).clone() {
                return Err(error);
            }
            Ok(guard(&self.trades).clone())
        })
    }

    fn fetch_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>> {
        Box::pin(async move {
            self.record(DataType::Quote, symbols, start, end);
            if self.quote_failures.load(Ordering::SeqCst) > 0 {
                self.quote_failures.fetch_sub(1, Ordering::SeqCst);
                return Err(ProviderError::Transport(
                    "temporary DNS failure".to_string(),
                ));
            }
            if let Some(error) = guard(&self.quote_error).clone() {
                return Err(error);
            }
            Ok(guard(&self.quotes).clone())
        })
    }
}

/// Deterministic test fake for the crypto provider boundary.
pub struct FakeCryptoProvider {
    pub trades: Mutex<Vec<NormalizedCryptoTrade>>,
    pub quotes: Mutex<Vec<NormalizedCryptoQuote>>,
    pub trade_error: Mutex<Option<ProviderError>>,
    pub quote_error: Mutex<Option<ProviderError>>,
    trade_failures: AtomicU32,
    quote_failures: AtomicU32,
    pub calls: Mutex<Vec<ProviderCall>>,
}

impl FakeCryptoProvider {
    /// Build a fake returning the supplied records.
    pub fn new(trades: Vec<NormalizedCryptoTrade>, quotes: Vec<NormalizedCryptoQuote>) -> Self {
        Self {
            trades: Mutex::new(trades),
            quotes: Mutex::new(quotes),
            trade_error: Mutex::new(None),
            quote_error: Mutex::new(None),
            trade_failures: AtomicU32::new(0),
            quote_failures: AtomicU32::new(0),
            calls: Mutex::new(Vec::new()),
        }
    }

    /// Make the next `failures` trade fetches fail transiently.
    pub fn failing_trades(self, failures: u32) -> Self {
        self.trade_failures.store(failures, Ordering::SeqCst);
        self
    }

    /// Make every trade fetch fail permanently.
    pub fn with_trade_error(self, error: ProviderError) -> Self {
        *guard(&self.trade_error) = Some(error);
        self
    }

    /// Make every quote fetch fail permanently.
    pub fn with_quote_error(self, error: ProviderError) -> Self {
        *guard(&self.quote_error) = Some(error);
        self
    }

    /// The recorded provider calls.
    pub fn calls(&self) -> Vec<ProviderCall> {
        guard(&self.calls).clone()
    }

    /// The recorded call data types in request order.
    pub fn call_kinds(&self) -> Vec<DataType> {
        guard(&self.calls)
            .iter()
            .map(|call| call.data_type)
            .collect()
    }

    fn record(&self, data_type: DataType, symbols: &[String], start: DateTime, end: DateTime) {
        guard(&self.calls).push(ProviderCall {
            data_type,
            symbols: symbols.to_vec(),
            start,
            end,
        });
    }
}

impl CryptoDataProvider for FakeCryptoProvider {
    fn fetch_crypto_trades<'a>(
        &'a self,
        pairs: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoTrade>, ProviderError>> {
        Box::pin(async move {
            self.record(DataType::Trade, pairs, start, end);
            if self.trade_failures.load(Ordering::SeqCst) > 0 {
                self.trade_failures.fetch_sub(1, Ordering::SeqCst);
                return Err(ProviderError::Transport(
                    "temporary DNS failure".to_string(),
                ));
            }
            if let Some(error) = guard(&self.trade_error).clone() {
                return Err(error);
            }
            Ok(guard(&self.trades).clone())
        })
    }

    fn fetch_crypto_quotes<'a>(
        &'a self,
        pairs: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoQuote>, ProviderError>> {
        Box::pin(async move {
            self.record(DataType::Quote, pairs, start, end);
            if self.quote_failures.load(Ordering::SeqCst) > 0 {
                self.quote_failures.fetch_sub(1, Ordering::SeqCst);
                return Err(ProviderError::Transport(
                    "temporary DNS failure".to_string(),
                ));
            }
            if let Some(error) = guard(&self.quote_error).clone() {
                return Err(error);
            }
            Ok(guard(&self.quotes).clone())
        })
    }
}

/// Committer that records every commit and publishes nothing.
#[derive(Default)]
pub struct RecordingCommitter {
    commits: Mutex<Vec<(Vec<StagedParquetObject>, Vec<CoverageRecord>)>>,
}

impl RecordingCommitter {
    /// An empty recorder.
    pub fn new() -> Self {
        Self::default()
    }

    /// The recorded `(objects, coverage)` commits.
    pub fn commits(&self) -> Vec<(Vec<StagedParquetObject>, Vec<CoverageRecord>)> {
        guard(&self.commits).clone()
    }

    /// How many times the committer was called.
    pub fn commit_count(&self) -> usize {
        guard(&self.commits).len()
    }
}

impl AtomicIngestCommitter for RecordingCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        guard(&self.commits).push((objects.to_vec(), coverage.to_vec()));
        Ok(())
    }
}

/// One recorded `PartitionWriter::write` call with its rows.
#[derive(Clone, Debug)]
pub struct WriteCall {
    /// Session date passed to the writer.
    pub session_date: Date,
    /// Asset class passed to the writer.
    pub asset_class: AssetClass,
    /// Data type passed to the writer.
    pub data_type: DataType,
    /// Symbol passed to the writer.
    pub symbol: String,
    /// The rows passed to the writer, in order.
    pub records: Vec<Value>,
}

/// Partition writer that records rows and stages JSON payloads.
#[derive(Default)]
pub struct RecordingWriter {
    writes: Mutex<Vec<WriteCall>>,
    staged: Mutex<Vec<StagedParquetObject>>,
}

impl RecordingWriter {
    /// An empty writer.
    pub fn new() -> Self {
        Self::default()
    }

    /// The recorded writes in call order.
    pub fn writes(&self) -> Vec<WriteCall> {
        guard(&self.writes).clone()
    }

    /// The rows recorded for one data type.
    pub fn records_for(&self, data_type: DataType) -> Vec<Value> {
        guard(&self.writes)
            .iter()
            .find(|call| call.data_type == data_type)
            .map(|call| call.records.clone())
            .unwrap_or_default()
    }

    /// How many staged objects remain undrained.
    pub fn staged_len(&self) -> usize {
        guard(&self.staged).len()
    }
}

impl PartitionWriter for RecordingWriter {
    fn write(
        &self,
        records: &[Value],
        session_date: Date,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: &str,
    ) -> Result<ParquetMetadata, DataError> {
        let path = partition_path(session_date, asset_class, data_type, symbol)?;
        let payload = serde_json::to_vec(records).map_err(DataError::io)?;
        let metadata = ParquetMetadata {
            path,
            rows: records.len() as u64,
            bytes: payload.len() as u64,
            checksum: test_checksum(&payload),
        };
        guard(&self.writes).push(WriteCall {
            session_date,
            asset_class,
            data_type,
            symbol: symbol.to_string(),
            records: records.to_vec(),
        });
        guard(&self.staged).push(StagedParquetObject {
            metadata: metadata.clone(),
            payload,
        });
        Ok(metadata)
    }

    fn drain_staged(&self) -> Vec<StagedParquetObject> {
        std::mem::take(&mut *guard(&self.staged))
    }

    fn reset_staged(&self) {
        guard(&self.staged).clear();
    }
}

/// Deterministic, cheap stand-in for a sha256 hex digest.
pub fn test_checksum(payload: &[u8]) -> String {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    payload.hash(&mut hasher);
    format!("test-{:016x}", hasher.finish())
}

/// Committer that writes objects and coverage into real local state.
pub struct LocalCommitter {
    store: Arc<LocalFilesystemStore>,
    catalog: MemoryCatalogRepository,
    commits: Mutex<Vec<(Vec<StagedParquetObject>, Vec<CoverageRecord>)>>,
}

impl LocalCommitter {
    /// Build a committer over a tempdir-backed local store.
    pub fn new(store: Arc<LocalFilesystemStore>) -> Self {
        Self {
            store,
            catalog: MemoryCatalogRepository::new(),
            commits: Mutex::new(Vec::new()),
        }
    }

    /// The local object store.
    pub fn store(&self) -> &Arc<LocalFilesystemStore> {
        &self.store
    }

    /// The catalog receiving the coverage rows.
    pub fn catalog(&self) -> &MemoryCatalogRepository {
        &self.catalog
    }

    /// How many times the committer was called.
    pub fn commit_count(&self) -> usize {
        guard(&self.commits).len()
    }

    /// The recorded `(objects, coverage)` commits.
    pub fn commits(&self) -> Vec<(Vec<StagedParquetObject>, Vec<CoverageRecord>)> {
        guard(&self.commits).clone()
    }
}

impl AtomicIngestCommitter for LocalCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        for object in objects {
            self.store.replace(&object.metadata.path, &object.payload)?;
        }
        for record in coverage {
            self.catalog.upsert(record)?;
        }
        guard(&self.commits).push((objects.to_vec(), coverage.to_vec()));
        Ok(())
    }
}

/// Usage provider that always reports zero bytes.
#[derive(Default)]
pub struct ZeroUsage;

impl StorageUsageProvider for ZeroUsage {
    fn current_storage(&self) -> Result<u64, DataError> {
        Ok(0)
    }
}

/// Usage provider that returns each scripted value in turn.
#[derive(Default)]
pub struct ScriptedUsage {
    values: Mutex<VecDeque<u64>>,
}

impl ScriptedUsage {
    /// Build a scripted usage reader.
    pub fn new(values: impl IntoIterator<Item = u64>) -> Self {
        Self {
            values: Mutex::new(values.into_iter().collect()),
        }
    }
}

impl StorageUsageProvider for ScriptedUsage {
    fn current_storage(&self) -> Result<u64, DataError> {
        guard(&self.values)
            .pop_front()
            .ok_or_else(|| DataError::value("no scripted usage value left"))
    }
}

/// The retry budget used by tests: three attempts, no backoff delay.
pub fn failing_retry() -> TransientRetry {
    TransientRetry {
        attempts: 3,
        base_delay_seconds: 0.0,
    }
}

/// Build an ingest-pausing guard with zero current usage.
pub fn zero_usage_guard(capacity_bytes: u64) -> Arc<StorageCapGuard> {
    Arc::new(StorageCapGuard::new(
        StorageCapPolicy {
            capacity_bytes,
            pause_enqueue_at_capacity: false,
            pause_ingest_at_capacity: true,
        },
        Arc::new(ZeroUsage),
    ))
}

/// Fixed, resettable UTC clock for crypto partial-day tests.
pub struct FixedClock {
    value: Mutex<DateTime>,
}

impl FixedClock {
    /// Build a clock at one instant.
    pub fn new(value: DateTime) -> Arc<Self> {
        Arc::new(Self {
            value: Mutex::new(value),
        })
    }

    /// Move the clock.
    pub fn set(&self, value: DateTime) {
        *guard(&self.value) = value;
    }

    /// A [`CryptoClock`] handle reading this clock.
    pub fn clock(self: &Arc<Self>) -> CryptoClock {
        let clock = Arc::clone(self);
        Arc::new(move || *guard(&clock.value))
    }
}

/// Read one object back from a local store.
pub fn read_object(store: &LocalFilesystemStore, path: &str) -> Vec<u8> {
    store.read(path).expect("object exists")
}
