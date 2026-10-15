//! Snapshot integrity and reuse against real canonical Parquet bytes.

use bytes::Bytes;
use futures_util::future::BoxFuture;
use harness_market_reader::{
    CacheConfig, MarketDataReader, MarketDataSnapshot, Partition, PartitionSource, ReaderError,
    SnapshotRequest,
};
use sha2::{Digest, Sha256};
use std::sync::atomic::{AtomicUsize, Ordering};

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
        object_path: "date=2024-01-18/asset_class=equity/data_type=trade/AAPL.parquet".into(),
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
    files[0].object_path = format!(
        "date=2024-01-18/asset_class=equity/data_type={}/AAPL.parquet",
        data.value()
    );
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
