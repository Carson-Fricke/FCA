//! Integration coverage for schema-pinned Parquet partition writes.
//!
//! Every pinned dataset is written through a real [`LocalFilesystemStore`],
//! read back with the `parquet` Arrow reader, and checked for schema (including
//! metadata and nullability), logical values, compression, key-value metadata,
//! the sha256 checksum, and byte-for-byte determinism.

use std::collections::HashMap;
use std::io::{Seek, SeekFrom, Write};
use std::sync::Arc;

use arrow_array::{
    Array, Decimal128Array, ListArray, RecordBatch, StringArray, TimestampNanosecondArray,
    UInt32Array, UInt64Array,
};
use arrow_schema::Schema;
use lake_contracts::{partition_path, schema_for, AssetClass, DataType, Date, Decimal18};
use lake_storage::{
    normalize_records, LocalFilesystemStore, ParquetMetadata, ParquetWriter, ReadableBlobStore,
};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::basic::Compression;
use parquet::file::metadata::ParquetMetaData;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tempfile::TempDir;

struct DatasetCase {
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &'static str,
    records: Vec<Value>,
}

fn session_date() -> Date {
    Date::from_ymd(2024, 1, 19).expect("session date")
}

fn datasets() -> Vec<DatasetCase> {
    vec![
        DatasetCase {
            asset_class: AssetClass::Equity,
            data_type: DataType::Trade,
            symbol: "AAPL",
            records: vec![
                json!({
                    "timestamp": "2024-01-19T14:30:00.123456789Z",
                    "symbol": "AAPL",
                    "trade_id": 7,
                    "exchange": "XNAS",
                    "price": "190.125",
                    "size": 100,
                    "conditions": ["@"],
                    "tape": "C",
                    "update": null,
                }),
                json!({
                    "timestamp": "2024-01-19T14:30:01Z",
                    "symbol": " aapl ",
                    "trade_id": 8,
                    "exchange": "XNAS",
                    "price": 191,
                    "size": 200,
                    "conditions": ["@", "F"],
                    "tape": "C",
                    "update": "T",
                }),
            ],
        },
        DatasetCase {
            asset_class: AssetClass::Equity,
            data_type: DataType::Quote,
            symbol: "AAPL",
            records: vec![json!({
                "timestamp": "2024-01-19T14:30:00Z",
                "symbol": "AAPL",
                "bid_exchange": "XNAS",
                "bid_price": "190.10",
                "bid_size": 100,
                "ask_exchange": "XNAS",
                "ask_price": "190.20",
                "ask_size": 200,
                "conditions": ["R"],
                "tape": "C",
            })],
        },
        DatasetCase {
            asset_class: AssetClass::Option,
            data_type: DataType::Trade,
            symbol: "AAPL240119C00150000",
            records: vec![json!({
                "timestamp": "2024-01-19T14:30:00Z",
                "symbol": "AAPL240119C00150000",
                "exchange": "XOPR",
                "price": "1.25",
                "size": 10,
                "condition": "I",
            })],
        },
        DatasetCase {
            asset_class: AssetClass::Option,
            data_type: DataType::Quote,
            symbol: "AAPL240119C00150000",
            records: vec![json!({
                "timestamp": "2024-01-19T14:30:00Z",
                "symbol": "AAPL240119C00150000",
                "bid_exchange": "XOPR",
                "bid_price": "1.20",
                "bid_size": 10,
                "ask_exchange": "XOPR",
                "ask_price": "1.30",
                "ask_size": 20,
                "condition": "I",
            })],
        },
        DatasetCase {
            asset_class: AssetClass::Crypto,
            data_type: DataType::Trade,
            symbol: "BTC/USD",
            records: vec![json!({
                "timestamp": "2024-01-19T14:30:00.123456789Z",
                "symbol": "BTC/USD",
                "trade_id": null,
                "exchange": null,
                "price": "41234.123456789012345678",
                "size": "0.000000010000000000",
                "taker_side": null,
            })],
        },
        DatasetCase {
            asset_class: AssetClass::Crypto,
            data_type: DataType::Quote,
            symbol: "BTC/USD",
            records: vec![json!({
                "timestamp": "2024-01-19T14:30:00Z",
                "symbol": "BTC/USD",
                "bid_exchange": null,
                "bid_price": "41234.12",
                "bid_size": "0.1",
                "ask_exchange": null,
                "ask_price": "41235.12",
                "ask_size": "0.2",
            })],
        },
    ]
}

fn write_case(
    case: &DatasetCase,
) -> (Arc<LocalFilesystemStore>, TempDir, ParquetMetadata, Vec<u8>) {
    let directory = tempfile::tempdir().expect("temp directory");
    let store = Arc::new(LocalFilesystemStore::new(directory.path()));
    let writer = ParquetWriter::new(Arc::clone(&store));
    let metadata = writer
        .write(
            &case.records,
            session_date(),
            case.asset_class,
            case.data_type,
            None,
            case.symbol,
        )
        .expect("write");
    let payload = store.read(&metadata.path).expect("read");
    (store, directory, metadata, payload)
}

fn read_back(payload: &[u8]) -> (Schema, Arc<ParquetMetaData>, Vec<RecordBatch>) {
    let mut file = tempfile::tempfile().expect("temp file");
    file.write_all(payload).expect("write payload");
    file.seek(SeekFrom::Start(0)).expect("seek");
    let builder = ParquetRecordBatchReaderBuilder::try_new(file).expect("parquet builder");
    let schema = builder.schema().as_ref().clone();
    let metadata = Arc::clone(builder.metadata());
    let batches = builder
        .build()
        .expect("reader")
        .collect::<Result<Vec<_>, _>>()
        .expect("batches");
    (schema, metadata, batches)
}

fn key_values(metadata: &ParquetMetaData) -> HashMap<&str, &str> {
    metadata
        .file_metadata()
        .key_value_metadata()
        .map(|entries| {
            entries
                .iter()
                .filter_map(|entry| {
                    entry
                        .value
                        .as_deref()
                        .map(|value| (entry.key.as_str(), value))
                })
                .collect()
        })
        .unwrap_or_default()
}

fn schema_name(asset_class: AssetClass, data_type: DataType) -> String {
    schema_for(asset_class, data_type)
        .expect("schema")
        .metadata()
        .get("lake.schema")
        .cloned()
        .expect("schema name")
}

fn assert_batches_equal(actual: &RecordBatch, expected: &RecordBatch) {
    // The reader rebuilds batch schemas from the Parquet fields, so batch
    // schemas carry no custom metadata; the file schema (asserted by the
    // caller) is the metadata-bearing one.
    assert_eq!(actual.schema().fields(), expected.schema().fields());
    for (index, column) in actual.columns().iter().enumerate() {
        assert_eq!(
            column.as_ref(),
            expected.column(index).as_ref(),
            "column {index} differs"
        );
    }
}

#[test]
fn every_pinned_dataset_round_trips_schema_values_metadata_and_checksum() {
    for case in datasets() {
        let (store, _directory, metadata, payload) = write_case(&case);
        let expected_path = partition_path(
            session_date(),
            case.asset_class,
            case.data_type,
            None,
            case.symbol,
        )
        .expect("path");

        assert_eq!(metadata.path, expected_path);
        assert_eq!(metadata.object_path(), expected_path);
        assert_eq!(metadata.rows, case.records.len() as u64);
        assert_eq!(metadata.bytes, payload.len() as u64);
        assert_eq!(metadata.checksum, hex::encode(Sha256::digest(&payload)));
        assert_eq!(store.read(&metadata.path).expect("read"), payload);

        let (schema, parquet_metadata, batches) = read_back(&payload);
        let expected_schema = schema_for(case.asset_class, case.data_type).expect("schema");

        let key_values = key_values(&parquet_metadata);
        assert_eq!(
            key_values.get("lake.schema").copied(),
            Some(schema_name(case.asset_class, case.data_type).as_str()),
            "lake.schema for {}",
            metadata.path
        );
        assert_eq!(
            key_values.get("lake.schema.version").copied(),
            Some("1"),
            "lake.schema.version for {}",
            metadata.path
        );

        assert_eq!(schema, expected_schema, "schema for {}", metadata.path);
        assert_eq!(
            schema
                .metadata()
                .get("lake.schema.version")
                .map(String::as_str),
            Some("1")
        );

        let expected =
            normalize_records(&case.records, case.asset_class, case.data_type, case.symbol)
                .expect("normalized");
        assert_eq!(batches.len(), 1);
        assert_eq!(batches[0].num_rows(), case.records.len());
        assert_batches_equal(&batches[0], &expected);

        let row_group = parquet_metadata.row_group(0);
        for index in 0..row_group.num_columns() {
            assert_eq!(
                row_group.column(index).compression(),
                Compression::ZSTD(Default::default()),
                "compression for {} column {index}",
                metadata.path
            );
        }

        let expected_nullable: Vec<bool> = expected_schema
            .fields()
            .iter()
            .map(|field| field.is_nullable())
            .collect();
        let actual_nullable: Vec<bool> = schema
            .fields()
            .iter()
            .map(|field| field.is_nullable())
            .collect();
        assert_eq!(actual_nullable, expected_nullable);
    }
}

#[test]
fn equity_trade_reads_back_logical_values_verbatim() {
    let case = &datasets()[0];
    let (_store, _directory, _metadata, payload) = write_case(case);
    let (_schema, _parquet_metadata, batches) = read_back(&payload);
    let batch = &batches[0];

    let timestamps = batch
        .column_by_name("timestamp")
        .expect("timestamp")
        .as_any()
        .downcast_ref::<TimestampNanosecondArray>()
        .expect("timestamp array");
    assert_eq!(timestamps.timezone(), Some("UTC"));
    assert_eq!(timestamps.value(0), 1_705_674_600_123_456_789);
    assert_eq!(timestamps.value(1), 1_705_674_601_000_000_000);

    let symbols = batch
        .column_by_name("symbol")
        .expect("symbol")
        .as_any()
        .downcast_ref::<StringArray>()
        .expect("symbol array");
    assert_eq!(symbols.value(0), "AAPL");
    assert_eq!(symbols.value(1), "AAPL");

    let trade_ids = batch
        .column_by_name("trade_id")
        .expect("trade_id")
        .as_any()
        .downcast_ref::<UInt64Array>()
        .expect("trade_id array");
    assert_eq!(trade_ids.value(1), 8);

    let prices = batch
        .column_by_name("price")
        .expect("price")
        .as_any()
        .downcast_ref::<Decimal128Array>()
        .expect("price array");
    assert_eq!(
        prices.value(0),
        Decimal18::parse("190.125").expect("decimal").unscaled()
    );
    assert_eq!(
        prices.value(1),
        Decimal18::parse("191").expect("decimal").unscaled()
    );

    let sizes = batch
        .column_by_name("size")
        .expect("size")
        .as_any()
        .downcast_ref::<UInt32Array>()
        .expect("size array");
    assert_eq!(sizes.value(1), 200);

    let conditions = batch
        .column_by_name("conditions")
        .expect("conditions")
        .as_any()
        .downcast_ref::<ListArray>()
        .expect("conditions array");
    let second = conditions.value(1);
    let second = second
        .as_any()
        .downcast_ref::<StringArray>()
        .expect("utf8 child");
    assert_eq!(second.len(), 2);
    assert_eq!(second.value(0), "@");
    assert_eq!(second.value(1), "F");
}

#[test]
fn rewriting_the_same_rows_produces_identical_bytes() {
    for case in datasets() {
        let (_first_store, _first_directory, first, first_payload) = write_case(&case);
        let (_second_store, _second_directory, second, second_payload) = write_case(&case);

        assert_eq!(first.path, second.path);
        assert_eq!(
            first_payload, second_payload,
            "payload for {} is not deterministic",
            first.path
        );
        assert_eq!(first.checksum, second.checksum);
        assert_eq!(first.rows, second.rows);
        assert_eq!(first.bytes, second.bytes);
    }
}
