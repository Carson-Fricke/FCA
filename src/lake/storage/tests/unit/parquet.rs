use super::*;
use arrow_array::Array;
use serde_json::json;

fn session_date() -> Date {
    Date::from_ymd(2024, 1, 19).expect("session date")
}

fn field_shape(batch: &RecordBatch) -> Vec<(String, String, bool)> {
    batch
        .schema()
        .fields()
        .iter()
        .map(|field| {
            (
                field.name().clone(),
                format!("{}", field.data_type()),
                field.is_nullable(),
            )
        })
        .collect()
}

fn column<'a, T: 'static>(batch: &'a RecordBatch, name: &str) -> &'a T {
    batch
        .column_by_name(name)
        .expect("column")
        .as_any()
        .downcast_ref::<T>()
        .expect("column type")
}

fn list_at(batch: &RecordBatch, name: &str, row: usize) -> Vec<String> {
    let list = column::<ListArray>(batch, name);
    let values = list.value(row);
    let strings = values
        .as_any()
        .downcast_ref::<StringArray>()
        .expect("utf8 child");
    (0..strings.len())
        .map(|index| strings.value(index).to_string())
        .collect()
}

fn decimal(text: &str) -> i128 {
    Decimal18::parse(text).expect("decimal").unscaled()
}

fn normalize_error(
    records: &[Value],
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
) -> DataError {
    match normalize_records(records, asset_class, data_type, symbol) {
        Ok(batch) => panic!(
            "normalization unexpectedly succeeded with {} rows",
            batch.num_rows()
        ),
        Err(error) => error,
    }
}

fn equity_trade(symbol: &str, price: Value) -> Value {
    json!({
        "timestamp": "2024-01-19T14:30:00.123456789Z",
        "symbol": symbol,
        "trade_id": 7,
        "exchange": "XNAS",
        "price": price,
        "size": 100,
        "conditions": ["@"],
        "tape": "C",
        "update": null,
    })
}

fn crypto_trade() -> Value {
    json!({
        "timestamp": "2024-01-19T14:30:00.123456789Z",
        "symbol": "BTC/USD",
        "trade_id": null,
        "exchange": null,
        "price": "41234.123456789012345678",
        "size": "0.000000010000000000",
        "taker_side": null,
    })
}

#[test]
fn equity_trade_normalizes_to_the_pinned_schema_and_values() {
    let batch = normalize_records(
        &[equity_trade(" aapl ", json!("190.125"))],
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
    )
    .expect("batch");

    assert_eq!(
        field_shape(&batch),
        vec![
            (
                "timestamp".to_string(),
                "Timestamp(ns, \"UTC\")".to_string(),
                false
            ),
            ("symbol".to_string(), "Utf8".to_string(), false),
            ("trade_id".to_string(), "UInt64".to_string(), false),
            ("exchange".to_string(), "Utf8".to_string(), false),
            ("price".to_string(), "Decimal128(38, 18)".to_string(), false),
            ("size".to_string(), "UInt32".to_string(), false),
            (
                "conditions".to_string(),
                "List(non-null Utf8)".to_string(),
                false
            ),
            ("tape".to_string(), "Utf8".to_string(), false),
            ("update".to_string(), "Utf8".to_string(), true),
        ]
    );
    assert_eq!(batch.num_rows(), 1);

    let timestamp = column::<TimestampNanosecondArray>(&batch, "timestamp");
    assert_eq!(timestamp.timezone(), Some("UTC"));
    assert_eq!(timestamp.value(0), 1_705_674_600_123_456_789);
    assert_eq!(column::<StringArray>(&batch, "symbol").value(0), "AAPL");
    assert_eq!(column::<UInt64Array>(&batch, "trade_id").value(0), 7);
    assert_eq!(column::<StringArray>(&batch, "exchange").value(0), "XNAS");
    assert_eq!(
        column::<Decimal128Array>(&batch, "price").value(0),
        decimal("190.125")
    );
    assert_eq!(column::<UInt32Array>(&batch, "size").value(0), 100);
    assert_eq!(list_at(&batch, "conditions", 0), vec!["@"]);
    assert_eq!(column::<StringArray>(&batch, "tape").value(0), "C");
    assert!(column::<StringArray>(&batch, "update").is_null(0));
    assert_eq!(
        batch
            .schema()
            .metadata()
            .get(SCHEMA_METADATA_KEY)
            .map(String::as_str),
        Some("equity.trade")
    );
}

#[test]
fn equity_quote_normalizes_to_the_pinned_schema_and_values() {
    let record = json!({
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
    });
    let batch =
        normalize_records(&[record], AssetClass::Equity, DataType::Quote, "AAPL").expect("batch");

    assert_eq!(
        field_shape(&batch),
        vec![
            (
                "timestamp".to_string(),
                "Timestamp(ns, \"UTC\")".to_string(),
                false
            ),
            ("symbol".to_string(), "Utf8".to_string(), false),
            ("bid_exchange".to_string(), "Utf8".to_string(), false),
            (
                "bid_price".to_string(),
                "Decimal128(38, 18)".to_string(),
                false
            ),
            ("bid_size".to_string(), "UInt32".to_string(), false),
            ("ask_exchange".to_string(), "Utf8".to_string(), false),
            (
                "ask_price".to_string(),
                "Decimal128(38, 18)".to_string(),
                false
            ),
            ("ask_size".to_string(), "UInt32".to_string(), false),
            (
                "conditions".to_string(),
                "List(non-null Utf8)".to_string(),
                false
            ),
            ("tape".to_string(), "Utf8".to_string(), false),
        ]
    );
    assert_eq!(
        column::<TimestampNanosecondArray>(&batch, "timestamp").value(0),
        1_705_674_600_000_000_000
    );
    assert_eq!(
        column::<Decimal128Array>(&batch, "bid_price").value(0),
        decimal("190.10")
    );
    assert_eq!(
        column::<Decimal128Array>(&batch, "ask_price").value(0),
        decimal("190.20")
    );
    assert_eq!(column::<UInt32Array>(&batch, "bid_size").value(0), 100);
    assert_eq!(column::<UInt32Array>(&batch, "ask_size").value(0), 200);
    assert_eq!(list_at(&batch, "conditions", 0), vec!["R"]);
    assert_eq!(
        batch
            .schema()
            .metadata()
            .get(SCHEMA_METADATA_KEY)
            .map(String::as_str),
        Some("equity.quote")
    );
}

#[test]
fn option_datasets_normalize_to_the_pinned_schemas_and_values() {
    let trade = json!({
        "timestamp": "2024-01-19T14:30:00Z",
        "symbol": "AAPL240119C00150000",
        "exchange": "XOPR",
        "price": "1.25",
        "size": 10,
        "condition": "I",
    });
    let batch = normalize_records(
        &[trade],
        AssetClass::Option,
        DataType::Trade,
        "AAPL240119C00150000",
    )
    .expect("option trade");
    assert_eq!(
        field_shape(&batch),
        vec![
            (
                "timestamp".to_string(),
                "Timestamp(ns, \"UTC\")".to_string(),
                false
            ),
            ("symbol".to_string(), "Utf8".to_string(), false),
            ("exchange".to_string(), "Utf8".to_string(), false),
            ("price".to_string(), "Decimal128(38, 18)".to_string(), false),
            ("size".to_string(), "UInt32".to_string(), false),
            ("condition".to_string(), "Utf8".to_string(), false),
        ]
    );
    assert_eq!(
        column::<Decimal128Array>(&batch, "price").value(0),
        decimal("1.25")
    );
    assert_eq!(column::<UInt32Array>(&batch, "size").value(0), 10);
    assert_eq!(column::<StringArray>(&batch, "condition").value(0), "I");

    let quote = json!({
        "timestamp": "2024-01-19T14:30:00Z",
        "symbol": "AAPL240119C00150000",
        "bid_exchange": "XOPR",
        "bid_price": "1.20",
        "bid_size": 10,
        "ask_exchange": "XOPR",
        "ask_price": "1.30",
        "ask_size": 20,
        "condition": "I",
    });
    let batch = normalize_records(
        &[quote],
        AssetClass::Option,
        DataType::Quote,
        "AAPL240119C00150000",
    )
    .expect("option quote");
    assert_eq!(
        field_shape(&batch),
        vec![
            (
                "timestamp".to_string(),
                "Timestamp(ns, \"UTC\")".to_string(),
                false
            ),
            ("symbol".to_string(), "Utf8".to_string(), false),
            ("bid_exchange".to_string(), "Utf8".to_string(), false),
            (
                "bid_price".to_string(),
                "Decimal128(38, 18)".to_string(),
                false
            ),
            ("bid_size".to_string(), "UInt32".to_string(), false),
            ("ask_exchange".to_string(), "Utf8".to_string(), false),
            (
                "ask_price".to_string(),
                "Decimal128(38, 18)".to_string(),
                false
            ),
            ("ask_size".to_string(), "UInt32".to_string(), false),
            ("condition".to_string(), "Utf8".to_string(), false),
        ]
    );
    assert_eq!(
        column::<Decimal128Array>(&batch, "bid_price").value(0),
        decimal("1.20")
    );
    assert_eq!(
        column::<Decimal128Array>(&batch, "ask_price").value(0),
        decimal("1.30")
    );
    assert_eq!(column::<UInt32Array>(&batch, "ask_size").value(0), 20);
}

#[test]
fn crypto_trade_normalizes_nullable_provider_fields_and_exact_decimals() {
    let batch = normalize_records(
        &[crypto_trade()],
        AssetClass::Crypto,
        DataType::Trade,
        "btc/usd",
    )
    .expect("crypto trade");

    assert_eq!(
        field_shape(&batch),
        vec![
            (
                "timestamp".to_string(),
                "Timestamp(ns, \"UTC\")".to_string(),
                false
            ),
            ("symbol".to_string(), "Utf8".to_string(), false),
            ("trade_id".to_string(), "Int64".to_string(), true),
            ("exchange".to_string(), "Utf8".to_string(), true),
            ("price".to_string(), "Decimal128(38, 18)".to_string(), false),
            ("size".to_string(), "Decimal128(38, 18)".to_string(), false),
            ("taker_side".to_string(), "Utf8".to_string(), true),
        ]
    );
    assert_eq!(column::<StringArray>(&batch, "symbol").value(0), "BTC/USD");
    assert!(column::<Int64Array>(&batch, "trade_id").is_null(0));
    assert!(column::<StringArray>(&batch, "exchange").is_null(0));
    assert!(column::<StringArray>(&batch, "taker_side").is_null(0));
    assert_eq!(
        column::<Decimal128Array>(&batch, "price").value(0),
        decimal("41234.123456789012345678")
    );
    assert_eq!(
        column::<Decimal128Array>(&batch, "size").value(0),
        decimal("0.000000010000000000")
    );
    assert_eq!(
        batch
            .schema()
            .metadata()
            .get(SCHEMA_METADATA_KEY)
            .map(String::as_str),
        Some("crypto.trade")
    );
}

#[test]
fn crypto_quote_normalizes_nullable_exchanges_and_decimal_sizes() {
    let record = json!({
        "timestamp": "2024-01-19T14:30:00Z",
        "symbol": "BTC/USD",
        "bid_exchange": null,
        "bid_price": "41234.12",
        "bid_size": "0.1",
        "ask_exchange": null,
        "ask_price": "41235.12",
        "ask_size": "0.2",
    });
    let batch = normalize_records(&[record], AssetClass::Crypto, DataType::Quote, "BTC/USD")
        .expect("crypto quote");

    assert_eq!(
        field_shape(&batch),
        vec![
            (
                "timestamp".to_string(),
                "Timestamp(ns, \"UTC\")".to_string(),
                false
            ),
            ("symbol".to_string(), "Utf8".to_string(), false),
            ("bid_exchange".to_string(), "Utf8".to_string(), true),
            (
                "bid_price".to_string(),
                "Decimal128(38, 18)".to_string(),
                false
            ),
            (
                "bid_size".to_string(),
                "Decimal128(38, 18)".to_string(),
                false
            ),
            ("ask_exchange".to_string(), "Utf8".to_string(), true),
            (
                "ask_price".to_string(),
                "Decimal128(38, 18)".to_string(),
                false
            ),
            (
                "ask_size".to_string(),
                "Decimal128(38, 18)".to_string(),
                false
            ),
        ]
    );
    assert!(column::<StringArray>(&batch, "bid_exchange").is_null(0));
    assert!(column::<StringArray>(&batch, "ask_exchange").is_null(0));
    assert_eq!(
        column::<Decimal128Array>(&batch, "bid_size").value(0),
        decimal("0.1")
    );
    assert_eq!(
        column::<Decimal128Array>(&batch, "ask_size").value(0),
        decimal("0.2")
    );
}

#[test]
fn empty_record_sets_produce_an_empty_batch() {
    let batch = normalize_records(&[], AssetClass::Equity, DataType::Trade, "AAPL").expect("batch");
    assert_eq!(batch.num_rows(), 0);
    assert_eq!(batch.num_columns(), 9);
}

#[test]
fn ignores_unknown_record_keys() {
    let mut record = equity_trade("AAPL", json!("190.125"));
    record["provider_noise"] = json!({"nested": true});
    record["another_key"] = json!(42);
    let batch =
        normalize_records(&[record], AssetClass::Equity, DataType::Trade, "AAPL").expect("batch");
    assert_eq!(batch.num_rows(), 1);
}

#[test]
fn requires_every_non_nullable_field() {
    let mut record = equity_trade("AAPL", json!("190.125"));
    record.as_object_mut().expect("object").remove("price");
    let error = normalize_error(&[record], AssetClass::Equity, DataType::Trade, "AAPL");
    assert!(
        matches!(&error, DataError::ValueError(message) if message.contains("missing required field 'price'")),
        "{error:?}"
    );

    let error = normalize_error(
        &[equity_trade("AAPL", Value::Null)],
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
    );
    assert!(
        matches!(&error, DataError::ValueError(message) if message.contains("missing required field 'price'")),
        "{error:?}"
    );
}

#[test]
fn rejects_symbols_that_do_not_match_the_partition() {
    let error = normalize_error(
        &[equity_trade("MSFT", json!("190.125"))],
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
    );
    assert!(
        matches!(&error, DataError::ValueError(message) if message.contains("does not match partition symbol")),
        "{error:?}"
    );

    let mut record = equity_trade("AAPL", json!("190.125"));
    record["symbol"] = json!(42);
    let error = normalize_error(&[record], AssetClass::Equity, DataType::Trade, "AAPL");
    assert!(
        matches!(&error, DataError::TypeError(message) if message.contains("symbol must be a string")),
        "{error:?}"
    );
}

#[test]
fn rejects_invalid_partition_symbols() {
    let error = normalize_error(&[], AssetClass::Equity, DataType::Trade, "!!");
    assert!(
        matches!(&error, DataError::ValueError(message) if message.contains("invalid market symbol")),
        "{error:?}"
    );
}

#[test]
fn rejects_float_and_bool_decimals_with_type_errors() {
    for price in [json!(190.125), json!(true)] {
        let error = normalize_error(
            &[equity_trade("AAPL", price)],
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
        );
        assert!(
            matches!(&error, DataError::TypeError(message) if message.contains("exact decimal")),
            "{error:?}"
        );
    }

    let error = normalize_error(
        &[equity_trade("AAPL", json!("not-a-decimal"))],
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
    );
    assert!(
        matches!(&error, DataError::ValueError(message) if message.contains("exact decimal")),
        "{error:?}"
    );
}

#[test]
fn accepts_integer_decimal_values() {
    let batch = normalize_records(
        &[equity_trade("AAPL", json!(190))],
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
    )
    .expect("batch");
    assert_eq!(
        column::<Decimal128Array>(&batch, "price").value(0),
        decimal("190")
    );
}

#[test]
fn parses_iso_and_integer_timestamps_at_nanosecond_precision() {
    let timestamp_of = |value: Value| {
        let mut record = equity_trade("AAPL", json!("190.125"));
        record["timestamp"] = value;
        let batch = normalize_records(&[record], AssetClass::Equity, DataType::Trade, "AAPL")
            .expect("batch");
        column::<TimestampNanosecondArray>(&batch, "timestamp").value(0)
    };

    assert_eq!(
        timestamp_of(json!("2024-01-19T14:30:00.123456789Z")),
        1_705_674_600_123_456_789
    );
    assert_eq!(
        timestamp_of(json!("2024-01-19T14:30:00.123456789+00:00")),
        1_705_674_600_123_456_789
    );
    assert_eq!(
        timestamp_of(json!("2024-01-19T14:30:00Z")),
        1_705_674_600_000_000_000
    );
    assert_eq!(
        timestamp_of(json!("2024-01-19T09:30:00.5-05:00")),
        1_705_674_600_500_000_000
    );
    assert_eq!(
        timestamp_of(json!(1_705_674_600_123_456_789i64)),
        1_705_674_600_123_456_789
    );
    assert_eq!(
        timestamp_of(json!("2024-01-19T14:30:00.5Z")),
        1_705_674_600_500_000_000
    );
    assert_eq!(
        timestamp_of(json!("2024-01-19T14:30:00.1234567890Z")),
        1_705_674_600_123_456_000
    );

    let mut record = equity_trade("AAPL", json!("190.125"));
    record["timestamp"] = json!("2024-01-19T14:30:00");
    let error = normalize_error(&[record], AssetClass::Equity, DataType::Trade, "AAPL");
    assert!(matches!(&error, DataError::ValueError(_)), "{error:?}");

    for value in [json!(1.5), json!(true)] {
        let mut record = equity_trade("AAPL", json!("190.125"));
        record["timestamp"] = value;
        let error = normalize_error(&[record], AssetClass::Equity, DataType::Trade, "AAPL");
        assert!(
            matches!(&error, DataError::TypeError(message) if message.contains("aware datetime or UTC nanoseconds")),
            "{error:?}"
        );
    }
}

#[test]
fn write_uses_the_pinned_partition_path() {
    struct RecordingStore(std::sync::Mutex<Vec<(String, Vec<u8>)>>);
    impl BlobStore for RecordingStore {
        fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError> {
            self.0
                .lock()
                .expect("lock")
                .push((relative_path.to_string(), payload.to_vec()));
            Ok(())
        }
    }

    let store = Arc::new(RecordingStore(std::sync::Mutex::new(Vec::new())));
    let writer = ParquetWriter::new(Arc::clone(&store));
    let metadata = writer
        .write(
            &[equity_trade("aapl", json!("190.125"))],
            session_date(),
            AssetClass::Equity,
            DataType::Trade,
            "aapl",
        )
        .expect("write");

    assert_eq!(
        metadata.path,
        "date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet"
    );
    assert_eq!(metadata.rows, 1);
    assert_eq!(metadata.object_path(), metadata.path);
    let objects = store.0.lock().expect("lock");
    assert_eq!(objects.len(), 1);
    assert_eq!(objects[0].0, metadata.path);
    assert_eq!(objects[0].1.len() as u64, metadata.bytes);
}
