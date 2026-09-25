//! End-to-end staging-to-local-store coverage for the ingest transactions.

mod support;

use std::sync::Arc;

use lake_catalog::{CatalogRepository, CoverageQuery};
use lake_contracts::DataType;
use lake_ingestion::{CryptoIngestTransaction, EquityIngestTransaction, StagingPartitionWriter};
use lake_storage::LocalFilesystemStore;
use support::{
    crypto_day, crypto_quote, crypto_trade, day, equity_quote, equity_session, equity_trade,
    instant, read_object, FakeCryptoProvider, FakeEquityProvider, LocalCommitter,
};

fn assert_parquet_object(store: &LocalFilesystemStore, path: &str, expected: &[u8]) {
    let bytes = read_object(store, path);
    assert_eq!(bytes, expected);
    assert!(bytes.starts_with(b"PAR1"));
    assert!(bytes.ends_with(b"PAR1"));
    assert!(bytes.len() > 8);
}

#[tokio::test]
async fn staged_equity_objects_reach_the_local_store_and_catalog() {
    let directory = tempfile::tempdir().expect("temp directory");
    let store = Arc::new(LocalFilesystemStore::new(directory.path()));
    let committer = Arc::new(LocalCommitter::new(Arc::clone(&store)));
    let transaction = EquityIngestTransaction::new(
        Arc::new(FakeEquityProvider::new(
            vec![
                equity_trade(instant(14, 31, 0, 0), 2, "AAPL"),
                equity_trade(instant(14, 30, 0, 0), 1, "AAPL"),
            ],
            vec![equity_quote(instant(14, 30, 0, 123_456), "AAPL")],
        )),
        Arc::new(StagingPartitionWriter::new()),
        committer.clone(),
    );

    let result = transaction
        .run("AAPL", &equity_session(false), None)
        .await
        .expect("successful transaction");
    assert_eq!(committer.commit_count(), 1);
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.row_count)
            .collect::<Vec<_>>(),
        vec![2, 1]
    );

    let commits = committer.commits();
    for (record, object) in commits[0].1.iter().zip(&commits[0].0) {
        let path = record.object_path.as_deref().expect("object path");
        assert_eq!(path, object.metadata.path);
        assert_parquet_object(&store, path, &object.payload);
        assert_eq!(record.byte_size, object.payload.len() as u64);
        assert_eq!(
            record.checksum.as_deref(),
            Some(object.metadata.checksum.as_str())
        );
        let checksum = record.checksum.as_deref().expect("checksum");
        assert_eq!(checksum.len(), 64);
        assert!(checksum
            .chars()
            .all(|character| character.is_ascii_hexdigit()));
    }
    assert_eq!(
        committer
            .catalog()
            .list(&CoverageQuery::default())
            .expect("catalog list")
            .len(),
        2
    );

    let first_payloads: Vec<Vec<u8>> = commits[0]
        .0
        .iter()
        .map(|object| object.payload.clone())
        .collect();
    let second = transaction
        .run("AAPL", &equity_session(false), None)
        .await
        .expect("repeated transaction");
    assert_eq!(second.objects, result.objects);
    let second_commits = committer.commits();
    let second_payloads: Vec<Vec<u8>> = second_commits[1]
        .0
        .iter()
        .map(|object| object.payload.clone())
        .collect();
    assert_eq!(first_payloads, second_payloads);
    assert_eq!(committer.commit_count(), 2);
}

#[tokio::test]
async fn staged_crypto_objects_reach_the_local_store_and_catalog() {
    let directory = tempfile::tempdir().expect("temp directory");
    let store = Arc::new(LocalFilesystemStore::new(directory.path()));
    let committer = Arc::new(LocalCommitter::new(Arc::clone(&store)));
    let transaction = CryptoIngestTransaction::new(
        Arc::new(FakeCryptoProvider::new(
            vec![crypto_trade(instant(0, 1, 0, 0), Some(1), "BTC/USD")],
            vec![crypto_quote(instant(0, 1, 0, 0), "BTC/USD")],
        )),
        Arc::new(StagingPartitionWriter::new()),
        committer.clone(),
    );

    let result = transaction
        .run("BTC/USD", &crypto_day(day(2024, 1, 19), false), None)
        .await
        .expect("successful transaction");
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.data_type)
            .collect::<Vec<_>>(),
        vec![DataType::Trade, DataType::Quote]
    );

    let commits = committer.commits();
    assert_eq!(commits[0].0.len(), 2);
    for (record, object) in commits[0].1.iter().zip(&commits[0].0) {
        let path = record.object_path.as_deref().expect("object path");
        assert!(path.starts_with("date=2024-01-19/asset_class=crypto/"));
        assert_parquet_object(&store, path, &object.payload);
    }
    assert_eq!(
        committer
            .catalog()
            .list(&CoverageQuery::default())
            .expect("catalog list")
            .len(),
        2
    );
}
