//! Streaming Arrow IPC read route behavior.

mod support;

use std::sync::Arc;

use arrow_array::{Array, Decimal128Array, RecordBatch, StringArray, TimestampNanosecondArray};
use arrow_ipc::reader::StreamReader;
use arrow_schema::Schema;
use axum::http::{header, StatusCode};
use http_body_util::BodyExt;
use lake_catalog::CatalogRepository;
use lake_contracts::{schema_for, AssetClass, DataType, Date};
use serde_json::json;
use support::*;
use tower::ServiceExt;

fn date(month: u32, day: u32) -> Date {
    Date::from_ymd(2024, month, day).expect("valid date")
}

fn decode_stream(bytes: &[u8]) -> (Schema, Vec<RecordBatch>) {
    let reader = StreamReader::try_new(std::io::Cursor::new(bytes), None).expect("ipc stream");
    let schema = reader.schema().as_ref().clone();
    let batches = reader
        .collect::<Result<Vec<_>, _>>()
        .expect("record batches");
    (schema, batches)
}

fn column<'a, T: 'static>(batch: &'a RecordBatch, name: &str) -> &'a T {
    batch
        .column_by_name(name)
        .expect("column")
        .as_any()
        .downcast_ref::<T>()
        .expect("array type")
}

#[tokio::test]
async fn read_streams_trades_with_the_frozen_schema_and_canonical_row_order() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let store = Arc::new(lake_storage::LocalFilesystemStore::new(temporary.path()));
    write_market_partition(
        &store,
        AssetClass::Equity,
        DataType::Trade,
        None,
        "AAPL",
        date(1, 18),
        &[
            json!({"timestamp": "2024-01-18T14:30:00Z", "trade_id": 1, "exchange": "XNAS", "price": "190.12", "size": 10, "conditions": ["@"], "tape": "C"}),
            json!({"timestamp": "2024-01-18T14:31:00Z", "trade_id": 2, "exchange": "XNAS", "price": "190.13", "size": 20, "conditions": ["@"], "tape": "C"}),
        ],
    );
    write_market_partition(
        &store,
        AssetClass::Equity,
        DataType::Trade,
        None,
        "AAPL",
        date(1, 19),
        &[
            json!({"timestamp": "2024-01-19T14:30:00Z", "trade_id": 3, "exchange": "XNAS", "price": "191.00", "size": 30, "conditions": ["@"], "tape": "C"}),
        ],
    );
    let catalog = shared_catalog([
        complete_record(AssetClass::Equity, DataType::Trade, "AAPL", date(1, 18)),
        complete_record(AssetClass::Equity, DataType::Trade, "AAPL", date(1, 19)),
    ]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn CatalogRepository>);
    state.read_store = Some(Arc::clone(&store) as Arc<dyn lake_storage::ReadableBlobStore>);
    let router = build_app(state);

    let path = "/v1/read?asset_class=equity&data_type=trade&symbol=aapl&start_date=2024-01-18&end_date=2024-01-19";
    let (status, headers, bytes) = send_bytes(&router, get_auth(path)).await;

    assert_eq!(status, StatusCode::OK, "{bytes:?}");
    assert_eq!(
        headers
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok()),
        Some("application/vnd.apache.arrow.stream")
    );
    let (schema, batches) = decode_stream(&bytes);
    let expected = schema_for(AssetClass::Equity, DataType::Trade).expect("frozen schema");
    assert_eq!(schema.fields(), expected.fields());
    assert_eq!(
        schema.metadata().get("lake.schema"),
        Some(&"equity.trade".to_string())
    );

    let timestamps: Vec<i64> = batches
        .iter()
        .flat_map(|batch| {
            column::<TimestampNanosecondArray>(batch, "timestamp")
                .values()
                .to_vec()
        })
        .collect();
    let trade_ids: Vec<u64> = batches
        .iter()
        .flat_map(|batch| {
            column::<arrow_array::UInt64Array>(batch, "trade_id")
                .values()
                .to_vec()
        })
        .collect();
    let expected_timestamps: Vec<i64> = [
        (date(1, 18), 14u32, 30u32),
        (date(1, 18), 14u32, 31u32),
        (date(1, 19), 14u32, 30u32),
    ]
    .iter()
    .map(|(session, hour, minute)| {
        lake_contracts::DateTime::from_ymd_hms(
            session.ymd().0,
            session.ymd().1,
            session.ymd().2,
            *hour,
            *minute,
            0,
            0,
        )
        .expect("instant")
        .to_nanos()
    })
    .collect();
    assert_eq!(timestamps, expected_timestamps);
    assert_eq!(trade_ids, vec![1, 2, 3]);
    assert!(batches.iter().all(|batch| {
        let symbols = column::<StringArray>(batch, "symbol");
        (0..symbols.len()).all(|index| symbols.value(index) == "AAPL")
    }));
}

#[tokio::test]
async fn read_streams_bars_and_enforces_the_interval_pairing() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let store = Arc::new(lake_storage::LocalFilesystemStore::new(temporary.path()));
    write_market_partition(
        &store,
        AssetClass::Equity,
        DataType::Bars,
        Some("1min"),
        "AAPL",
        date(1, 18),
        &[
            json!({"timestamp": "2024-01-18T14:31:00Z", "open": "190.12", "high": "190.55", "low": "189.95", "close": "190.41", "volume": "1200"}),
            json!({"timestamp": "2024-01-18T14:32:00Z", "open": "190.41", "high": "190.60", "low": "190.30", "close": "190.55", "volume": "1500"}),
        ],
    );
    let catalog = shared_catalog([complete_bars_record("1min", "AAPL", date(1, 18))]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn CatalogRepository>);
    state.read_store = Some(Arc::clone(&store) as Arc<dyn lake_storage::ReadableBlobStore>);
    let router = build_app(state);

    let path = "/v1/read?asset_class=equity&data_type=bars&symbol=AAPL&interval=1min&start_date=2024-01-18&end_date=2024-01-18";
    let (status, _, bytes) = send_bytes(&router, get_auth(path)).await;
    assert_eq!(status, StatusCode::OK, "{bytes:?}");
    let (schema, batches) = decode_stream(&bytes);
    let expected = schema_for(AssetClass::Equity, DataType::Bars).expect("frozen schema");
    assert_eq!(schema.fields(), expected.fields());
    assert_eq!(
        schema.metadata().get("lake.schema"),
        Some(&"equity.bars".to_string())
    );
    let closes: Vec<String> = batches
        .iter()
        .flat_map(|batch| {
            let values = column::<Decimal128Array>(batch, "close");
            (0..values.len())
                .map(|index| {
                    lake_contracts::Decimal18::from_unscaled_unchecked(values.value(index))
                        .to_string()
                })
                .collect::<Vec<_>>()
        })
        .collect();
    assert_eq!(closes, vec!["190.41", "190.55"]);

    let missing_interval =
        "/v1/read?asset_class=equity&data_type=bars&symbol=AAPL&start_date=2024-01-18&end_date=2024-01-18";
    let (status, _, body) = send(&router, get_auth(missing_interval)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "validation_error");

    let trade_with_interval =
        "/v1/read?asset_class=equity&data_type=trade&symbol=AAPL&interval=1min&start_date=2024-01-18&end_date=2024-01-18";
    let (status, _, _) = send_bytes(&router, get_auth(trade_with_interval)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);

    let unsupported_type =
        "/v1/read?asset_class=equity&data_type=classification&symbol=AAPL&start_date=2024-01-18&end_date=2024-01-18";
    let (status, _, _) = send_bytes(&router, get_auth(unsupported_type)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);

    let unsupported_asset =
        "/v1/read?asset_class=reference&data_type=trade&symbol=AAPL&start_date=2024-01-18&end_date=2024-01-18";
    let (status, _, _) = send_bytes(&router, get_auth(unsupported_asset)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
}

#[tokio::test]
async fn read_without_ensured_files_is_no_data() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let store = Arc::new(lake_storage::LocalFilesystemStore::new(temporary.path()));
    let catalog = shared_catalog([status_record(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        date(1, 18),
        lake_catalog::CoverageStatus::Empty,
    )]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn CatalogRepository>);
    state.read_store = Some(Arc::clone(&store) as Arc<dyn lake_storage::ReadableBlobStore>);
    let router = build_app(state);

    let path = "/v1/read?asset_class=equity&data_type=trade&symbol=AAPL&start_date=2024-01-18&end_date=2024-01-18";
    let (status, _, body) = send(&router, get_auth(path)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["code"], "no_data");
}

#[tokio::test]
async fn read_requires_a_bearer_token() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let store = Arc::new(lake_storage::LocalFilesystemStore::new(temporary.path()));
    write_market_partition(
        &store,
        AssetClass::Equity,
        DataType::Trade,
        None,
        "AAPL",
        date(1, 18),
        &[
            json!({"timestamp": "2024-01-18T14:30:00Z", "trade_id": 1, "exchange": "XNAS", "price": "190.12", "size": 10, "conditions": ["@"], "tape": "C"}),
        ],
    );
    let catalog = shared_catalog([complete_record(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        date(1, 18),
    )]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn CatalogRepository>);
    state.read_store = Some(Arc::clone(&store) as Arc<dyn lake_storage::ReadableBlobStore>);
    let router = build_app(state);

    let path = "/v1/read?asset_class=equity&data_type=trade&symbol=AAPL&start_date=2024-01-18&end_date=2024-01-18";
    let (status, headers, _) = send_bytes(&router, get(path)).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(
        headers
            .get(header::WWW_AUTHENTICATE)
            .and_then(|value| value.to_str().ok()),
        Some("Bearer")
    );
}

#[tokio::test]
async fn large_reads_coalesce_bounded_frames_without_changing_ipc_bytes() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let store = Arc::new(lake_storage::LocalFilesystemStore::new(temporary.path()));
    let rows: Vec<_> = (0..4096)
        .map(|id| json!({"timestamp": "2024-01-18T14:30:00Z", "trade_id": id, "exchange": "XNAS", "price": "190.12", "size": 10, "conditions": ["@"], "tape": "C"}))
        .collect();
    write_market_partition(
        &store,
        AssetClass::Equity,
        DataType::Trade,
        None,
        "AAPL",
        date(1, 18),
        &rows,
    );
    let record = complete_record(AssetClass::Equity, DataType::Trade, "AAPL", date(1, 18));
    let mut expected = Vec::new();
    lake_storage::write_dataset_ipc_stream(
        &*store,
        &[record.object_path.clone().expect("object path")],
        AssetClass::Equity,
        DataType::Trade,
        &mut expected,
    )
    .expect("expected stream");
    let catalog = shared_catalog([record]);
    let mut state = base_state(catalog);
    state.read_store = Some(store);
    let router = build_app(state);
    let request = get_auth("/v1/read?asset_class=equity&data_type=trade&symbol=AAPL&start_date=2024-01-18&end_date=2024-01-18");
    let response = router.oneshot(request).await.expect("response");
    assert_eq!(response.status(), StatusCode::OK);
    let mut body = response.into_body();
    let mut bytes = Vec::new();
    let mut frames = 0;
    while let Some(frame) = body.frame().await {
        let frame = frame.expect("stream frame");
        if let Some(data) = frame.data_ref() {
            assert!(data.len() <= 64 * 1024, "bounded response buffer");
            frames += 1;
            bytes.extend_from_slice(data);
        }
    }
    assert!(
        frames < 32,
        "must coalesce small Arrow writes, got {frames} frames"
    );
    assert_eq!(
        bytes, expected,
        "transport buffering must preserve canonical IPC bytes and EOF"
    );
    let (_, batches) = decode_stream(&bytes);
    assert_eq!(
        batches.iter().map(RecordBatch::num_rows).sum::<usize>(),
        rows.len()
    );
}

#[tokio::test]
async fn failed_partition_reads_remain_stream_failures() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let store = Arc::new(lake_storage::LocalFilesystemStore::new(temporary.path()));
    let catalog = shared_catalog([complete_record(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        date(1, 18),
    )]);
    let mut state = base_state(catalog);
    state.read_store = Some(store);
    let router = build_app(state);
    let request = get_auth("/v1/read?asset_class=equity&data_type=trade&symbol=AAPL&start_date=2024-01-18&end_date=2024-01-18");
    let response = router.oneshot(request).await.expect("response");
    assert_eq!(response.status(), StatusCode::OK);
    assert!(
        response.into_body().collect().await.is_err(),
        "missing storage payload must not become a successful partial stream"
    );
}
