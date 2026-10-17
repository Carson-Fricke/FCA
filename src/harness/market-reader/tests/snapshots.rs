//! Snapshot integrity and reuse against real canonical Parquet bytes.

use bytes::Bytes;
use futures_util::future::BoxFuture;
use harness_market_reader::{
    CacheConfig, MarketDataReader, MarketDataSnapshot, Partition, PartitionSource, ReaderError,
    SnapshotRequest,
};
use sha2::{Digest, Sha256};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

struct Source {
    payload: Bytes,
    reads: AtomicUsize,
}
impl PartitionSource for Source {
    fn read<'a>(&'a self, _: &'a Partition) -> BoxFuture<'a, Result<Bytes, ReaderError>> {
        Box::pin(async move {
            self.reads.fetch_add(1, Ordering::SeqCst);
            tokio::task::yield_now().await;
            Ok(self.payload.clone())
        })
    }
}

fn fixture() -> (Source, MarketDataSnapshot) {
    let payload = Bytes::from_static(include_bytes!(
        "../../../lake/storage/tests/oracle/parquet/equity_trade.parquet"
    ));
    let request = SnapshotRequest {
        asset_class: "equity".into(),
        data_type: "trade".into(),
        symbol: "AAPL".into(),
        start_date: "2024-01-18".into(),
        end_date: "2024-01-18".into(),
        interval: None,
    };
    let partition = Partition {
        session_date: "2024-01-18".into(),
        object_path: lake_contracts::immutable_path(
            "date=2024-01-18/asset_class=equity/data_type=trade/AAPL.parquet",
            &hex::encode(Sha256::digest(&payload)),
        )
        .unwrap(),
        checksum: hex::encode(Sha256::digest(&payload)),
        row_count: 2,
        byte_size: payload.len() as u64,
    };
    (
        Source {
            payload,
            reads: AtomicUsize::new(0),
        },
        MarketDataSnapshot::new(request, vec![partition]).unwrap(),
    )
}

async fn collect(
    reader: &MarketDataReader,
    snapshot: &MarketDataSnapshot,
    source: &Source,
) -> Result<Vec<arrow_array::RecordBatch>, ReaderError> {
    let mut batches = Vec::new();
    reader
        .visit(snapshot, source, |batch| {
            batches.push(batch);
            Ok(())
        })
        .await?;
    Ok(batches)
}

#[tokio::test]
async fn concurrent_and_restarted_readers_reuse_verified_partitions() {
    let directory = tempfile::tempdir().unwrap();
    let config = CacheConfig {
        directory: directory.path().to_path_buf(),
        disk_bytes: 1024 * 1024,
        memory_bytes: 1024 * 1024,
    };
    let reader = MarketDataReader::new(config.clone());
    let (source, snapshot) = fixture();
    let (first, second) = tokio::join!(
        collect(&reader, &snapshot, &source),
        collect(&reader, &snapshot, &source)
    );
    let first = first.unwrap();
    assert_eq!(
        first[0].column(0).to_data().buffers()[0].as_ptr(),
        second.unwrap()[0].column(0).to_data().buffers()[0].as_ptr()
    );
    assert_eq!(first.iter().map(|batch| batch.num_rows()).sum::<usize>(), 2);
    assert_eq!(source.reads.load(Ordering::SeqCst), 1);
    let mut inconsistent = snapshot.files.clone();
    inconsistent[0].row_count += 1;
    let inconsistent = MarketDataSnapshot::new(snapshot.request.clone(), inconsistent).unwrap();
    assert!(matches!(
        collect(&reader, &inconsistent, &source).await,
        Err(ReaderError::Integrity(_))
    ));
    let restarted = MarketDataReader::new(config);
    collect(&restarted, &snapshot, &source).await.unwrap();
    assert_eq!(source.reads.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn modified_objects_and_corrupt_cache_entries_cannot_satisfy_a_snapshot() {
    let directory = tempfile::tempdir().unwrap();
    let config = CacheConfig {
        directory: directory.path().to_path_buf(),
        disk_bytes: 1024 * 1024,
        memory_bytes: 0,
    };
    let reader = MarketDataReader::new(config.clone());
    let (mut source, snapshot) = fixture();
    source.payload = Bytes::from_static(b"changed object");
    assert!(matches!(
        collect(&reader, &snapshot, &source).await,
        Err(ReaderError::Integrity(_))
    ));
    assert_eq!(std::fs::read_dir(directory.path()).unwrap().count(), 0);
    std::fs::write(
        directory
            .path()
            .join(format!("{}.parquet", snapshot.files[0].checksum)),
        b"corrupt cache",
    )
    .unwrap();
    assert!(matches!(
        collect(&reader, &snapshot, &source).await,
        Err(ReaderError::Integrity(_))
    ));
    assert_eq!(source.reads.load(Ordering::SeqCst), 1);
}

#[test]
fn identities_pin_exact_metadata_and_reject_forged_or_duplicate_partitions() {
    let (_, snapshot) = fixture();
    let same = MarketDataSnapshot::new(snapshot.request.clone(), snapshot.files.clone()).unwrap();
    assert_eq!(snapshot.snapshot_id, same.snapshot_id);
    let mut forged = snapshot.clone();
    forged.files[0].row_count += 1;
    assert!(forged.validate().is_err());
    assert!(MarketDataSnapshot::new(
        snapshot.request.clone(),
        vec![snapshot.files[0].clone(), snapshot.files[0].clone()]
    )
    .is_err());
    let mut new_files = snapshot.files.clone();
    new_files[0].checksum = "0".repeat(64);
    assert!(MarketDataSnapshot::new(snapshot.request.clone(), new_files.clone()).is_err());
    let logical = snapshot.files[0].object_path.split_once('/').unwrap().1;
    new_files[0].object_path =
        lake_contracts::immutable_path(logical, &new_files[0].checksum).unwrap();
    let mut outdated = snapshot.files.clone();
    outdated[0].object_path = logical.to_string();
    assert!(MarketDataSnapshot::new(snapshot.request.clone(), outdated).is_err());
    assert_ne!(
        MarketDataSnapshot::new(snapshot.request, new_files)
            .unwrap()
            .snapshot_id,
        snapshot.snapshot_id
    );
}

#[tokio::test]
async fn disabled_caches_always_use_the_source_including_empty_partitions() {
    for (source, snapshot) in [fixture(), empty_fixture(lake_contracts::DataType::Trade)] {
        let directory = tempfile::tempdir().unwrap();
        let reader = MarketDataReader::new(CacheConfig {
            directory: directory.path().into(),
            disk_bytes: 0,
            memory_bytes: 0,
        });
        for _ in 0..2 {
            collect(&reader, &snapshot, &source).await.unwrap();
        }
        assert_eq!(source.reads.load(Ordering::SeqCst), 2);
        assert_eq!(std::fs::read_dir(directory.path()).unwrap().count(), 0);
    }
}

fn empty_fixture(data: lake_contracts::DataType) -> (Source, MarketDataSnapshot) {
    let (mut source, mut snapshot) = fixture();
    let schema = lake_contracts::schema_for(lake_contracts::AssetClass::Equity, data).unwrap();
    let writer =
        parquet::arrow::ArrowWriter::try_new(Vec::new(), std::sync::Arc::new(schema), None)
            .unwrap();
    source.payload = Bytes::from(writer.into_inner().unwrap());
    snapshot.request.data_type = data.value().into();
    let mut files = snapshot.files;
    files[0].checksum = hex::encode(Sha256::digest(&source.payload));
    files[0].row_count = 0;
    files[0].byte_size = source.payload.len() as u64;
    files[0].object_path = lake_contracts::immutable_path(
        &format!(
            "date=2024-01-18/asset_class=equity/data_type={}/AAPL.parquet",
            data.value()
        ),
        &files[0].checksum,
    )
    .unwrap();
    (
        source,
        MarketDataSnapshot::new(snapshot.request, files).unwrap(),
    )
}

#[tokio::test]
async fn empty_partitions_are_evicted_within_the_memory_budget() {
    let directory = tempfile::tempdir().unwrap();
    let reader = MarketDataReader::new(CacheConfig {
        directory: directory.path().into(),
        disk_bytes: 0,
        memory_bytes: 512,
    });
    let (trades, trade_snapshot) = empty_fixture(lake_contracts::DataType::Trade);
    let (quotes, quote_snapshot) = empty_fixture(lake_contracts::DataType::Quote);
    collect(&reader, &trade_snapshot, &trades).await.unwrap();
    collect(&reader, &trade_snapshot, &trades).await.unwrap();
    assert_eq!(trades.reads.load(Ordering::SeqCst), 1);
    collect(&reader, &quote_snapshot, &quotes).await.unwrap();
    collect(&reader, &trade_snapshot, &trades).await.unwrap();
    assert_eq!(trades.reads.load(Ordering::SeqCst), 2);
}

fn range_fixture() -> (Vec<Bytes>, MarketDataSnapshot) {
    let (source, snapshot) = fixture();
    let mut reader =
        parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder::try_new(source.payload)
            .unwrap()
            .build()
            .unwrap();
    let original = reader.next().unwrap().unwrap();
    let schema = Arc::new(
        lake_contracts::schema_for(
            lake_contracts::AssetClass::Equity,
            lake_contracts::DataType::Trade,
        )
        .unwrap(),
    );
    let mut files = Vec::new();
    let mut payloads = Vec::new();
    for index in 0..6 {
        let mut columns = original.columns().to_vec();
        columns[0] = Arc::new(
            arrow_array::TimestampNanosecondArray::from(vec![index; original.num_rows()])
                .with_timezone("UTC"),
        );
        let batch = arrow_array::RecordBatch::try_new(schema.clone(), columns).unwrap();
        let mut writer =
            parquet::arrow::ArrowWriter::try_new(Vec::new(), schema.clone(), None).unwrap();
        writer.write(&batch).unwrap();
        let payload = Bytes::from(writer.into_inner().unwrap());
        let date = format!("2024-01-{:02}", 18 + index);
        files.push(Partition {
            session_date: date.clone(),
            object_path: lake_contracts::immutable_path(
                &format!("date={date}/asset_class=equity/data_type=trade/AAPL.parquet"),
                &hex::encode(Sha256::digest(&payload)),
            )
            .unwrap(),
            checksum: hex::encode(Sha256::digest(&payload)),
            row_count: batch.num_rows() as u64,
            byte_size: payload.len() as u64,
        });
        payloads.push(payload);
    }
    let mut request = snapshot.request;
    request.end_date = "2024-01-23".into();
    (payloads, MarketDataSnapshot::new(request, files).unwrap())
}

struct ActiveRead<'a>(&'a AtomicUsize);

impl Drop for ActiveRead<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

struct RangeSource {
    payloads: Vec<Bytes>,
    overlap: tokio::sync::Barrier,
    second: tokio::sync::Notify,
    active: AtomicUsize,
    peak: AtomicUsize,
    completed: Mutex<Vec<usize>>,
    cancel_second: bool,
}

impl RangeSource {
    fn new(payloads: Vec<Bytes>, cancel_second: bool) -> Self {
        Self {
            payloads,
            overlap: tokio::sync::Barrier::new(2),
            second: tokio::sync::Notify::new(),
            active: AtomicUsize::new(0),
            peak: AtomicUsize::new(0),
            completed: Mutex::new(Vec::new()),
            cancel_second,
        }
    }
}

impl PartitionSource for RangeSource {
    fn read<'a>(&'a self, partition: &'a Partition) -> BoxFuture<'a, Result<Bytes, ReaderError>> {
        Box::pin(async move {
            let index = partition.session_date[8..].parse::<usize>().unwrap() - 18;
            let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
            self.peak.fetch_max(active, Ordering::SeqCst);
            let _active = ActiveRead(&self.active);
            if index < 2 {
                self.overlap.wait().await;
                if index == 0 {
                    self.second.notified().await;
                } else {
                    self.second.notify_one();
                    if self.cancel_second {
                        futures_util::future::pending::<()>().await;
                    }
                }
            }
            self.completed.lock().unwrap().push(index);
            Ok(self.payloads[index].clone())
        })
    }
}

#[tokio::test]
async fn partition_downloads_overlap_and_batches_keep_snapshot_order() {
    for memory_bytes in [0, 1, 1024 * 1024] {
        let directory = tempfile::tempdir().unwrap();
        let reader = MarketDataReader::new(CacheConfig {
            directory: directory.path().into(),
            disk_bytes: 0,
            memory_bytes,
        });
        let (payloads, snapshot) = range_fixture();
        let source = RangeSource::new(payloads, false);
        let mut visited = Vec::new();
        tokio::time::timeout(
            Duration::from_secs(2),
            reader.visit(&snapshot, &source, |batch| {
                if visited.is_empty() {
                    assert!(source.completed.lock().unwrap().len() <= 3);
                }
                visited.push(
                    batch
                        .column(0)
                        .as_any()
                        .downcast_ref::<arrow_array::TimestampNanosecondArray>()
                        .unwrap()
                        .value(0),
                );
                Ok(())
            }),
        )
        .await
        .expect("partition downloads must overlap")
        .unwrap();
        assert_eq!(visited, vec![0, 1, 2, 3, 4, 5]);
        assert_eq!(source.completed.lock().unwrap()[0], 1);
        assert!((2..=4).contains(&source.peak.load(Ordering::SeqCst)));
        assert_eq!(source.active.load(Ordering::SeqCst), 0);
    }
}

#[tokio::test]
async fn visitor_failure_cancels_pending_downloads_and_releases_capacity() {
    let directory = tempfile::tempdir().unwrap();
    let reader = MarketDataReader::new(CacheConfig {
        directory: directory.path().into(),
        disk_bytes: 0,
        memory_bytes: 0,
    });
    let (payloads, snapshot) = range_fixture();
    let source = RangeSource::new(payloads.clone(), true);
    let outcome = tokio::time::timeout(
        Duration::from_secs(2),
        reader.visit(&snapshot, &source, |_| {
            Err(ReaderError::Source("visitor stopped".into()))
        }),
    )
    .await
    .expect("visitor failure must cancel a stalled lookahead");
    assert!(matches!(outcome, Err(ReaderError::Source(message)) if message == "visitor stopped"));
    assert_eq!(source.active.load(Ordering::SeqCst), 0);
    {
        let completed = source.completed.lock().unwrap();
        assert!(completed.contains(&0));
        assert!(!completed.contains(&1));
    }
    let stalled = RangeSource::new(payloads.clone(), true);
    assert!(tokio::time::timeout(
        Duration::from_millis(25),
        reader.visit(&snapshot, &stalled, |_| Ok(())),
    )
    .await
    .is_err());
    assert_eq!(stalled.active.load(Ordering::SeqCst), 0);
    let source = RangeSource::new(payloads, false);
    tokio::time::timeout(
        Duration::from_secs(2),
        reader.visit(&snapshot, &source, |_| Ok(())),
    )
    .await
    .expect("cancellation must leave capacity for another complete read")
    .unwrap();
}

struct OversizedSource {
    first: Bytes,
    first_visited: AtomicBool,
    head_requested: AtomicBool,
}

impl PartitionSource for OversizedSource {
    fn read<'a>(&'a self, partition: &'a Partition) -> BoxFuture<'a, Result<Bytes, ReaderError>> {
        Box::pin(async move {
            if partition.session_date == "2024-01-18" {
                return Ok(self.first.clone());
            }
            assert!(
                self.first_visited.load(Ordering::SeqCst),
                "oversized object must wait until it becomes the required head"
            );
            self.head_requested.store(true, Ordering::SeqCst);
            // An unavailable object exercises the real head path without
            // allocating the oversized payload described by the manifest.
            Err(ReaderError::Source("oversized object unavailable".into()))
        })
    }
}

#[tokio::test]
async fn oversized_objects_are_read_only_when_they_become_the_ordered_head() {
    let directory = tempfile::tempdir().unwrap();
    let reader = MarketDataReader::new(CacheConfig {
        directory: directory.path().into(),
        disk_bytes: 0,
        memory_bytes: 0,
    });
    let (payloads, snapshot) = range_fixture();
    let mut files = snapshot.files[..2].to_vec();
    files[1].byte_size = 64 * 1024 * 1024 + 1;
    let snapshot = MarketDataSnapshot::new(snapshot.request, files).unwrap();
    let source = OversizedSource {
        first: payloads[0].clone(),
        first_visited: AtomicBool::new(false),
        head_requested: AtomicBool::new(false),
    };
    let outcome = reader
        .visit(&snapshot, &source, |_| {
            source.first_visited.store(true, Ordering::SeqCst);
            Ok(())
        })
        .await;
    assert!(
        matches!(outcome, Err(ReaderError::Source(message)) if message == "oversized object unavailable")
    );
    assert!(source.head_requested.load(Ordering::SeqCst));
}
