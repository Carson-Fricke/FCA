//! Private tests for the buffering backfill committer.

use std::sync::Arc;

use lake_catalog::{CatalogRepository, CoverageQuery, CoverageRecord, CoverageStatus};
use lake_contracts::{AssetClass, DataType, Date};
use lake_ingestion::{AtomicIngestCommitter, StagedParquetObject};
use lake_storage::{BlobStore, LocalFilesystemStore, ParquetMetadata};

use super::*;

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn coverage(symbol: &str, session_date: Date) -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        symbol,
        session_date,
        CoverageStatus::Complete,
        Some(format!(
            "date={}/asset_class=equity/data_type=trade/{symbol}.parquet",
            session_date.isoformat()
        )),
        1,
        7,
        Some("checksum".to_string()),
        None,
        None,
    )
    .expect("valid coverage")
}

fn staged(path: &str) -> StagedParquetObject {
    StagedParquetObject {
        metadata: ParquetMetadata {
            path: path.to_string(),
            rows: 1,
            bytes: 7,
            checksum: "checksum".to_string(),
        },
        payload: b"parquet".to_vec(),
    }
}

#[test]
fn backfill_committer_writes_objects_immediately_and_flushes_rows_in_batches() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let objects = temporary.path().join("objects");
    let catalog_path = temporary.path().join("state").join("catalog.json");
    let store: Arc<dyn BlobStore> = Arc::new(LocalFilesystemStore::new(&objects));
    let catalog =
        Arc::new(lake_catalog::JsonCatalogStore::open(&catalog_path).expect("open catalog"));
    let committer = LocalBackfillCommitter::new(store, catalog, 2);

    let first = staged("date=2024-01-18/asset_class=equity/data_type=trade/AAPL.parquet");
    let second = staged("date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet");
    let third = staged("date=2024-01-19/asset_class=equity/data_type=trade/MSFT.parquet");

    committer
        .commit(
            std::slice::from_ref(&first),
            &[coverage("AAPL", day(2024, 1, 18))],
        )
        .expect("first commit");
    // The object is visible immediately; the catalog waits for the batch.
    assert!(objects.join(&first.metadata.path).is_file());
    assert!(!catalog_path.is_file());

    committer
        .commit(
            std::slice::from_ref(&second),
            &[coverage("AAPL", day(2024, 1, 19))],
        )
        .expect("second commit");
    // Reaching the batch size flushes both buffered rows in one update.
    let flushed = lake_catalog::JsonCatalogStore::open(&catalog_path).expect("open catalog");
    let query = CoverageQuery::default();
    assert_eq!(flushed.list(&query).expect("list").len(), 2);

    committer
        .commit(
            std::slice::from_ref(&third),
            &[coverage("MSFT", day(2024, 1, 19))],
        )
        .expect("third commit");
    let buffered = lake_catalog::JsonCatalogStore::open(&catalog_path).expect("open catalog");
    assert_eq!(buffered.list(&query).expect("list").len(), 2);

    committer.flush().expect("final flush");
    let final_store = lake_catalog::JsonCatalogStore::open(&catalog_path).expect("open catalog");
    assert_eq!(final_store.list(&query).expect("list").len(), 3);
}
