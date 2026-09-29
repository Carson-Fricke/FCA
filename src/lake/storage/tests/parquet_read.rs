//! Typed trade-partition reads and the interval-aware bars write path.
//!
//! The reader is the derivation input boundary: it must restore the exact
//! decimals, timestamps, identifiers, and content revision from the pinned
//! Parquet objects.

use std::sync::Arc;

use lake_contracts::{AssetClass, BarInterval, DataType, Date, DateTime, Decimal18};
use lake_storage::{
    read_crypto_trades, read_equity_trades, CryptoTradeRecord, EquityTradeRecord,
    LocalFilesystemStore, ParquetWriter,
};
use serde_json::json;

fn session_date() -> Date {
    Date::from_ymd(2024, 11, 27).expect("date")
}

fn store() -> (Arc<LocalFilesystemStore>, tempfile::TempDir) {
    let directory = tempfile::tempdir().expect("temp directory");
    let store = Arc::new(LocalFilesystemStore::new(directory.path()));
    (store, directory)
}

#[test]
fn equity_trades_round_trip_with_exact_decimals_and_revisions() {
    let (store, _directory) = store();
    let writer = ParquetWriter::new(Arc::clone(&store));
    let records = vec![
        json!({
            "timestamp": "2024-11-27T14:30:00.123456789Z",
            "symbol": "AAPL",
            "trade_id": 12345,
            "exchange": "XNAS",
            "price": "190.123456789012345678",
            "size": 100,
            "conditions": ["@", "I"],
            "tape": "C",
            "update": null,
        }),
        json!({
            "timestamp": "2024-11-27T14:30:01Z",
            "symbol": "AAPL",
            "trade_id": 12346,
            "exchange": "XNAS",
            "price": "190.5",
            "size": 5,
            "conditions": [],
            "tape": "C",
            "update": "u1",
        }),
    ];
    let metadata = writer
        .write(
            &records,
            session_date(),
            AssetClass::Equity,
            DataType::Trade,
            None,
            "AAPL",
        )
        .expect("write");

    let partition = read_equity_trades(store.as_ref(), &metadata.path).expect("read");
    assert_eq!(partition.revision, metadata.checksum);
    assert_eq!(
        partition.records,
        vec![
            EquityTradeRecord {
                timestamp: DateTime::parse_iso_utc("2024-11-27T14:30:00.123456Z")
                    .expect("timestamp"),
                symbol: "AAPL".to_string(),
                trade_id: 12345,
                exchange: "XNAS".to_string(),
                price: Decimal18::parse("190.123456789012345678").expect("price"),
                size: 100,
                conditions: vec!["@".to_string(), "I".to_string()],
                tape: "C".to_string(),
                update: None,
            },
            EquityTradeRecord {
                timestamp: DateTime::parse_iso_utc("2024-11-27T14:30:01Z").expect("timestamp"),
                symbol: "AAPL".to_string(),
                trade_id: 12346,
                exchange: "XNAS".to_string(),
                price: Decimal18::parse("190.5").expect("price"),
                size: 5,
                conditions: Vec::new(),
                tape: "C".to_string(),
                update: Some("u1".to_string()),
            },
        ]
    );
}

#[test]
fn crypto_trades_round_trip_fractional_sizes() {
    let (store, _directory) = store();
    let writer = ParquetWriter::new(Arc::clone(&store));
    let records = vec![json!({
        "timestamp": 1_732_722_600_000_000_000i64,
        "symbol": "BTC/USD",
        "trade_id": null,
        "exchange": null,
        "price": "41234.123456789012345678",
        "size": "0.000000010000000000",
        "taker_side": null,
    })];
    let metadata = writer
        .write(
            &records,
            session_date(),
            AssetClass::Crypto,
            DataType::Trade,
            None,
            "BTC/USD",
        )
        .expect("write");

    let partition = read_crypto_trades(store.as_ref(), &metadata.path).expect("read");
    assert_eq!(partition.revision, metadata.checksum);
    assert_eq!(
        partition.records,
        vec![CryptoTradeRecord {
            timestamp: DateTime::from_nanos(1_732_722_600_000_000_000),
            symbol: "BTC/USD".to_string(),
            trade_id: None,
            exchange: None,
            price: Decimal18::parse("41234.123456789012345678").expect("price"),
            size: Decimal18::parse("0.000000010000000000").expect("size"),
            taker_side: None,
        }]
    );
}

#[test]
fn empty_trade_partitions_read_back_with_their_revision() {
    let (store, _directory) = store();
    let writer = ParquetWriter::new(Arc::clone(&store));
    let metadata = writer
        .write(
            &[],
            session_date(),
            AssetClass::Equity,
            DataType::Trade,
            None,
            "AAPL",
        )
        .expect("write");

    let partition = read_equity_trades(store.as_ref(), &metadata.path).expect("read");
    assert!(partition.records.is_empty());
    assert_eq!(partition.revision, metadata.checksum);
    assert_eq!(metadata.rows, 0);
}

#[test]
fn reading_the_wrong_dataset_is_rejected() {
    let (store, _directory) = store();
    let writer = ParquetWriter::new(Arc::clone(&store));
    let metadata = writer
        .write(
            &[json!({
                "timestamp": 1_732_722_600_000_000_000i64,
                "symbol": "BTC/USD",
                "trade_id": null,
                "exchange": null,
                "price": "1",
                "size": "1",
                "taker_side": null,
            })],
            session_date(),
            AssetClass::Crypto,
            DataType::Trade,
            None,
            "BTC/USD",
        )
        .expect("write");
    let error = read_equity_trades(store.as_ref(), &metadata.path).expect_err("wrong dataset");
    assert!(error.to_string().contains("equity.trade"), "{error}");
}

#[test]
fn bars_write_with_the_interval_partition_path() {
    let (store, _directory) = store();
    let writer = ParquetWriter::new(Arc::clone(&store));
    let metadata = writer
        .write(
            &[json!({
                "timestamp": "2024-11-27T14:30:00Z",
                "symbol": "AAPL",
                "open": "190.12",
                "high": "190.55",
                "low": "189.98",
                "close": "190.41",
                "volume": "1200",
            })],
            session_date(),
            AssetClass::Equity,
            DataType::Bars,
            Some(BarInterval::OneDay.value()),
            "AAPL",
        )
        .expect("write");
    assert_eq!(
        metadata.path,
        "date=2024-11-27/asset_class=equity/data_type=bars/interval=1d/AAPL.parquet"
    );
    assert_eq!(metadata.rows, 1);
}

#[test]
fn bars_without_an_interval_are_rejected_by_the_path_builder() {
    let (store, _directory) = store();
    let writer = ParquetWriter::new(Arc::clone(&store));
    let error = writer
        .write(
            &[],
            session_date(),
            AssetClass::Equity,
            DataType::Bars,
            None,
            "AAPL",
        )
        .expect_err("bars need an interval");
    assert!(error.to_string().contains("interval"), "{error}");
}
