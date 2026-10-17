//! Regression coverage for provider-bar session grouping.
//!
//! Provider minute bars are start-stamped, so the lake groups them on the
//! half-open session window `[open, close)`. The SIP row stamped exactly at the
//! close is the first extended-hours bar of the following period and must be
//! excluded instead of colliding with the final regular-session minute bucket
//! (RCA: QQQ 2025-05-30 falsification ensure failure).

use std::sync::{Arc, Mutex, MutexGuard};

use arrow_array::{Decimal128Array, RecordBatch, TimestampNanosecondArray};
use futures_util::future::BoxFuture;
use lake_catalog::{CoverageRecord, CoverageStatus};
use lake_contracts::{
    partition_path, AssetClass, BarInterval, DataError, DataType, Date, DateTime, Decimal18,
};
use lake_coverage::CoverageSession;
use lake_ingestion::{
    AtomicIngestCommitter, BarsDataProvider, BarsIngestTransaction, PartitionWriter,
    StagedParquetObject, StoredTradePartition, TradePartitionReader,
};
use lake_providers::{NormalizedBar, ProviderError};
use lake_storage::ParquetMetadata;

fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn instant(year: i32, month: u32, day_of_month: u32, hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, minute, 0, 0).expect("instant")
}

fn session(year: i32, month: u32, day_of_month: u32) -> CoverageSession {
    CoverageSession {
        session_date: Date::from_ymd(year, month, day_of_month).expect("date"),
        open_time: instant(year, month, day_of_month, 13, 30),
        close_time: instant(year, month, day_of_month, 20, 0),
        is_partial: false,
    }
}

fn bar(timestamp: DateTime, close: &str) -> NormalizedBar {
    NormalizedBar {
        timestamp,
        symbol: "QQQ".to_string(),
        open: Decimal18::parse("519.11").expect("open"),
        high: Decimal18::parse("519.99").expect("high"),
        low: Decimal18::parse("519.00").expect("low"),
        close: Decimal18::parse(close).expect("close"),
        volume: Decimal18::parse("100").expect("volume"),
    }
}

struct FakeBarsProvider {
    bars: Vec<NormalizedBar>,
}

impl BarsDataProvider for FakeBarsProvider {
    fn fetch_bars<'a>(
        &'a self,
        _asset_class: AssetClass,
        _symbol: &'a str,
        _interval: BarInterval,
        _start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedBar>, ProviderError>> {
        Box::pin(async move { Ok(self.bars.clone()) })
    }
}

struct EmptyTradeReader;

impl TradePartitionReader for EmptyTradeReader {
    fn read<'a>(
        &'a self,
        _asset_class: AssetClass,
        _symbol: &'a str,
        _session_date: Date,
    ) -> BoxFuture<'a, Result<Option<StoredTradePartition>, DataError>> {
        Box::pin(async move { Ok(None) })
    }
}

#[derive(Clone, Debug)]
struct WriteCall {
    records: RecordBatch,
}

#[derive(Default)]
struct RecordingWriter {
    writes: Mutex<Vec<WriteCall>>,
    staged: Mutex<Vec<StagedParquetObject>>,
}

impl RecordingWriter {
    fn writes(&self) -> Vec<WriteCall> {
        guard(&self.writes).clone()
    }
}

impl PartitionWriter for RecordingWriter {
    fn write(
        &self,
        records: &RecordBatch,
        session_date: Date,
        asset_class: AssetClass,
        data_type: DataType,
        interval: Option<&str>,
        symbol: &str,
    ) -> Result<ParquetMetadata, DataError> {
        let path = partition_path(session_date, asset_class, data_type, interval, symbol)?;
        let payload = Vec::new();
        let metadata = ParquetMetadata {
            path,
            rows: records.num_rows() as u64,
            bytes: payload.len() as u64,
            checksum: "test-checksum".to_string(),
        };
        guard(&self.writes).push(WriteCall {
            records: records.clone(),
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

#[derive(Default)]
struct RecordingCommitter {
    commits: Mutex<Vec<(Vec<StagedParquetObject>, Vec<CoverageRecord>)>>,
}

impl RecordingCommitter {
    fn commit_count(&self) -> usize {
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

fn transaction(
    bars: Vec<NormalizedBar>,
) -> (
    BarsIngestTransaction,
    Arc<RecordingWriter>,
    Arc<RecordingCommitter>,
) {
    let writer = Arc::new(RecordingWriter::default());
    let committer = Arc::new(RecordingCommitter::default());
    let provider = Arc::new(FakeBarsProvider { bars });
    let reader = Arc::new(EmptyTradeReader);
    let transaction =
        BarsIngestTransaction::new(provider, reader, writer.clone(), committer.clone());
    (transaction, writer, committer)
}

#[tokio::test]
async fn provider_bars_at_the_session_close_are_excluded() {
    let session = session(2025, 5, 30);
    let (transaction, writer, committer) = transaction(vec![
        bar(instant(2025, 5, 30, 8, 0), "519.00"),
        bar(instant(2025, 5, 30, 13, 30), "519.11"),
        bar(instant(2025, 5, 30, 19, 59), "519.13"),
        bar(instant(2025, 5, 30, 20, 0), "519.96"),
    ]);

    let result = transaction
        .run(
            AssetClass::Equity,
            "QQQ",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    assert_eq!(result.coverage[0].status, CoverageStatus::Complete);
    let writes = writer.writes();
    assert_eq!(writes.len(), 1);
    let records = &writes[0].records;
    assert_eq!(
        records.num_rows(),
        2,
        "only regular-session minutes are kept"
    );
    assert_eq!(
        Some(timestamp(records, 0)),
        Some(instant(2025, 5, 30, 13, 30).to_nanos_i64())
    );
    assert_eq!(
        Some(timestamp(records, 1)),
        Some(instant(2025, 5, 30, 19, 59).to_nanos_i64())
    );
    assert_eq!(close(records, 1), Decimal18::parse("519.13").unwrap());
    assert_eq!(result.coverage[0].row_count, 2);
    assert_eq!(committer.commit_count(), 1);
}

#[tokio::test]
async fn repeated_provider_bar_timestamps_in_one_bucket_still_error() {
    let session = session(2025, 5, 30);
    let (transaction, writer, committer) = transaction(vec![
        bar(instant(2025, 5, 30, 15, 0), "519.11"),
        bar(instant(2025, 5, 30, 15, 0), "519.12"),
    ]);

    let result = transaction
        .run(
            AssetClass::Equity,
            "QQQ",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    assert_eq!(result.coverage[0].status, CoverageStatus::Error);
    assert!(result.coverage[0]
        .error_message
        .as_deref()
        .expect("message")
        .contains("overlap a 1min bucket"));
    assert!(writer.writes().is_empty());
    assert_eq!(committer.commit_count(), 1);
}

#[tokio::test]
async fn each_session_in_a_union_window_drops_its_own_close_bar() {
    let sessions = [session(2025, 5, 30), session(2025, 6, 2)];
    let (transaction, writer, _committer) = transaction(vec![
        bar(instant(2025, 5, 30, 13, 30), "519.11"),
        bar(instant(2025, 5, 30, 19, 59), "519.13"),
        bar(instant(2025, 5, 30, 20, 0), "519.96"),
        bar(instant(2025, 5, 30, 20, 1), "519.97"),
        bar(instant(2025, 6, 2, 13, 30), "521.11"),
        bar(instant(2025, 6, 2, 19, 59), "521.13"),
        bar(instant(2025, 6, 2, 20, 0), "521.96"),
        bar(instant(2025, 6, 2, 20, 1), "521.97"),
    ]);

    let result = transaction
        .run(AssetClass::Equity, "QQQ", BarInterval::OneMinute, &sessions)
        .await
        .expect("run");

    assert_eq!(result.coverage.len(), 2);
    assert!(result
        .coverage
        .iter()
        .all(|record| record.status == CoverageStatus::Complete));
    let writes = writer.writes();
    assert_eq!(writes.len(), 2);
    assert!(writes.iter().all(|write| write.records.num_rows() == 2));
    assert_eq!(
        Some(timestamp(&writes[0].records, 1)),
        Some(instant(2025, 5, 30, 19, 59).to_nanos_i64())
    );
    assert_eq!(
        Some(timestamp(&writes[1].records, 1)),
        Some(instant(2025, 6, 2, 19, 59).to_nanos_i64())
    );
}

fn timestamp(batch: &RecordBatch, row: usize) -> i64 {
    batch
        .column_by_name("timestamp")
        .unwrap()
        .as_any()
        .downcast_ref::<TimestampNanosecondArray>()
        .unwrap()
        .value(row)
}

fn close(batch: &RecordBatch, row: usize) -> Decimal18 {
    let value = batch
        .column_by_name("close")
        .unwrap()
        .as_any()
        .downcast_ref::<Decimal128Array>()
        .unwrap()
        .value(row);
    Decimal18::from_unscaled(value).unwrap()
}

#[derive(Default)]
struct PublicationLeases {
    scopes: Mutex<Vec<lake_storage::SymbolDayScope>>,
    root: std::path::PathBuf,
}
impl lake_storage::LeaseProvider for PublicationLeases {
    fn acquire(
        &self,
        scope: &lake_storage::SymbolDayScope,
    ) -> Result<Box<dyn lake_storage::Lease>, DataError> {
        guard(&self.scopes).push(scope.clone());
        lake_storage::LeaseProvider::acquire(
            &lake_local::FileLeaseProvider(self.root.clone()),
            scope,
        )
    }
}
struct CountingProvider {
    provider: FakeBarsProvider,
    calls: std::sync::atomic::AtomicUsize,
}
impl BarsDataProvider for CountingProvider {
    fn fetch_bars<'a>(
        &'a self,
        asset: AssetClass,
        symbol: &'a str,
        interval: BarInterval,
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedBar>, ProviderError>> {
        self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        self.provider
            .fetch_bars(asset, symbol, interval, start, end)
    }
}

#[tokio::test]
async fn week_partitions_fetch_once_per_range_and_publish_every_session_under_its_own_lease() {
    use lake_catalog::{CatalogRepository, PublicationCatalog};
    use lake_coverage::{requested_coverage, EnsureJob, EnsureSpec};
    use lake_ingestion::{EnsureIngest, EnsureProviders, ObjectCatalogCommitter};
    use lake_storage::ReadableBlobStore;

    let start = Date::from_ymd(2025, 5, 27).unwrap();
    let provider = Arc::new(CountingProvider {
        provider: FakeBarsProvider {
            bars: (0..14)
                .map(|offset| {
                    let date = start.add_days(offset);
                    let timestamp =
                        DateTime::parse_iso_utc(&format!("{}T13:30:00Z", date.isoformat()))
                            .unwrap();
                    bar(timestamp, "519.13")
                })
                .collect(),
        },
        calls: std::sync::atomic::AtomicUsize::new(0),
    });
    let directory = tempfile::tempdir().unwrap();
    let objects = Arc::new(lake_storage::LocalFilesystemStore::new(directory.path()));
    let leases = Arc::new(PublicationLeases {
        scopes: Mutex::default(),
        root: directory.path().into(),
    });
    let catalog = Arc::new(PublicationCatalog::new(objects.clone(), leases.clone()));
    let as_of = DateTime::parse_iso_utc("2025-06-10T00:00:00Z").unwrap();
    let application = EnsureIngest {
        providers: EnsureProviders {
            bars: Some(provider.clone()),
            ..EnsureProviders::default()
        },
        contracts: None,
        committer: Arc::new(ObjectCatalogCommitter::new(
            objects.clone(),
            catalog.clone(),
        )),
        trades: Arc::new(EmptyTradeReader),
        clock: Arc::new(move || as_of),
    };
    let mut expected = Vec::new();
    for offset in [0, 7] {
        let request = EnsureSpec {
            asset_class: AssetClass::Equity,
            data_type: DataType::Bars,
            symbol: "QQQ".into(),
            start_date: start.add_days(offset),
            end_date: start.add_days(offset + 6),
            interval: Some("1min".into()),
        };
        let gaps = requested_coverage(catalog.as_ref(), None, &request, as_of, "ensure")
            .unwrap()
            .gap_plan()
            .gaps;
        assert!(
            gaps.len() > 1,
            "this seven-day partition contains multiple sessions"
        );
        expected.extend(gaps.iter().map(|gap| gap.session.session_date));
        application
            .run(
                &EnsureJob {
                    job_id: "multi-week-bars".into(),
                    request,
                    gaps,
                },
                None,
                2,
            )
            .await
            .unwrap();
    }
    assert_eq!(
        provider.calls.load(std::sync::atomic::Ordering::SeqCst),
        2,
        "provider acquisition stays batched by range"
    );
    assert_eq!(
        guard(&leases.scopes)
            .iter()
            .map(|scope| scope.session_date)
            .collect::<Vec<_>>(),
        expected
    );
    for date in expected {
        let record = catalog
            .get(
                AssetClass::Equity,
                DataType::Bars,
                Some("1min"),
                "QQQ",
                date,
            )
            .unwrap()
            .unwrap();
        assert_eq!(record.status, CoverageStatus::Complete);
        assert_eq!(record.row_count, 1);
        assert_eq!(record.bar_source, Some(lake_catalog::BarSource::Provider));
        assert_eq!(record.source_revision, record.checksum);
        assert_eq!(
            objects
                .read(record.object_path.as_deref().unwrap())
                .unwrap()
                .len() as u64,
            record.byte_size
        );
    }
}

#[test]
fn derived_crypto_midnight_belongs_only_to_the_following_utc_day() {
    let date = Date::from_ymd(2025, 1, 5).unwrap();
    let first = CoverageSession {
        session_date: date,
        open_time: DateTime::midnight(date),
        close_time: DateTime::midnight(date.add_days(1)),
        is_partial: false,
    };
    let second = CoverageSession {
        session_date: date.add_days(1),
        open_time: first.close_time,
        close_time: DateTime::midnight(date.add_days(2)),
        is_partial: false,
    };
    let records: Vec<_> = [(&first, "100", "1"), (&second, "200", "2")]
        .into_iter()
        .map(|(session, price, size)| lake_ingestion::CryptoTradeRecord {
            timestamp: session.open_time,
            symbol: "BTC/USD".into(),
            trade_id: None,
            exchange: None,
            price: Decimal18::parse(price).unwrap(),
            size: Decimal18::parse(size).unwrap(),
            taker_side: None,
        })
        .collect();
    for (session, price, volume) in [(first, "100", "1"), (second, "200", "2")] {
        let bars =
            lake_ingestion::derive_crypto_bars("BTC/USD", &records, BarInterval::OneDay, &session)
                .unwrap();
        assert_eq!(bars.len(), 1);
        assert_eq!(bars[0].timestamp, session.open_time);
        assert_eq!(bars[0].open, Decimal18::parse(price).unwrap());
        assert_eq!(bars[0].close, Decimal18::parse(price).unwrap());
        assert_eq!(bars[0].volume, Decimal18::parse(volume).unwrap());
    }
}

struct UncertainSecondWrite {
    inner: lake_storage::LocalFilesystemStore,
    calls: std::sync::atomic::AtomicUsize,
}
impl lake_storage::BlobStore for UncertainSecondWrite {
    fn replace(&self, path: &str, payload: &[u8]) -> Result<(), DataError> {
        self.inner.replace(path, payload)?;
        if self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst) == 1 {
            return Err(DataError::Io(
                "second object write acknowledgement lost".into(),
            ));
        }
        Ok(())
    }
}

#[tokio::test]
async fn later_session_publication_failure_preserves_prior_session_without_replay() {
    use lake_catalog::{CatalogRepository, PublicationCatalog};
    use lake_ingestion::{ObjectCatalogCommitter, StagingPartitionWriter};
    let sessions = [session(2025, 5, 30), session(2025, 6, 2)];
    let provider = Arc::new(CountingProvider {
        provider: FakeBarsProvider {
            bars: sessions
                .iter()
                .map(|session| bar(session.open_time, "519.13"))
                .collect(),
        },
        calls: std::sync::atomic::AtomicUsize::new(0),
    });
    let directory = tempfile::tempdir().unwrap();
    let objects = Arc::new(UncertainSecondWrite {
        inner: lake_storage::LocalFilesystemStore::new(directory.path()),
        calls: std::sync::atomic::AtomicUsize::new(0),
    });
    let catalog = Arc::new(PublicationCatalog::new(
        Arc::new(lake_storage::LocalFilesystemStore::new(directory.path())),
        Arc::new(lake_local::FileLeaseProvider(directory.path().into())),
    ));
    let transaction = BarsIngestTransaction::new(
        provider.clone(),
        Arc::new(EmptyTradeReader),
        Arc::new(StagingPartitionWriter::new()),
        Arc::new(ObjectCatalogCommitter::new(
            objects.clone(),
            catalog.clone(),
        )),
    );
    assert!(transaction
        .run(AssetClass::Equity, "QQQ", BarInterval::OneMinute, &sessions)
        .await
        .is_err());
    assert_eq!(provider.calls.load(std::sync::atomic::Ordering::SeqCst), 1);
    assert_eq!(objects.calls.load(std::sync::atomic::Ordering::SeqCst), 2);
    assert_eq!(
        catalog
            .get(
                AssetClass::Equity,
                DataType::Bars,
                Some("1min"),
                "QQQ",
                sessions[0].session_date
            )
            .unwrap()
            .unwrap()
            .status,
        CoverageStatus::Complete
    );
    assert!(catalog
        .get(
            AssetClass::Equity,
            DataType::Bars,
            Some("1min"),
            "QQQ",
            sessions[1].session_date
        )
        .unwrap()
        .is_none());
}
