//! Raw-object route behavior.

mod support;

use std::sync::Arc;

use axum::http::StatusCode;
use lake_catalog::{CoverageRecord, CoverageStatus, MemoryCatalogRepository};
use lake_contracts::{partition_path, AssetClass, DataType, Date};
use serde_json::json;
use support::*;

const SESSION_DATE: (u32, u32, u32) = (2024, 1, 18);

fn date(month: u32, day: u32) -> Date {
    Date::from_ymd(2024, month, day).expect("valid date")
}

fn canonical_path(
    session_date: Date,
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
) -> String {
    partition_path(session_date, asset_class, data_type, None, symbol).expect("canonical path")
}

fn bars_path(session_date: Date, interval: &str, symbol: &str) -> String {
    partition_path(
        session_date,
        AssetClass::Equity,
        DataType::Bars,
        Some(interval),
        symbol,
    )
    .expect("canonical bars path")
}

fn raw_record(object_path: Option<String>) -> CoverageRecord {
    let (_, month, day) = SESSION_DATE;
    let session_date = date(month, day);
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        session_date,
        CoverageStatus::Complete,
        Some(object_path.unwrap_or_else(|| {
            canonical_path(session_date, AssetClass::Equity, DataType::Trade, "AAPL")
        })),
        12,
        3456,
        Some("checksum".to_string()),
        None,
        None,
    )
    .expect("valid record")
}

fn raw_path(object_path: &str) -> String {
    format!("/v1/raw?object_path={}", encode_query_value(object_path))
}

#[tokio::test]
async fn authenticated_raw_object_returns_catalog_and_sas_metadata() {
    let object_path = canonical_path(date(1, 21), AssetClass::Crypto, DataType::Quote, "BTC/USD");
    let record = CoverageRecord::new(
        AssetClass::Crypto,
        DataType::Quote,
        "BTC/USD",
        date(1, 21),
        CoverageStatus::Complete,
        Some(object_path.clone()),
        24,
        6789,
        Some("crypto-checksum".to_string()),
        None,
        None,
    )
    .expect("valid record");
    let catalog = Arc::new(MemoryCatalogRepository::from_records([record]));
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(catalog);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let (status, _, body) = send(&router, get_auth(&raw_path(&object_path))).await;

    let url = format!("https://blob.test/{object_path}?read-only-expiring");
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body,
        json!({
            "asset_class": "crypto",
            "data_type": "quote",
            "symbol": "BTC/USD",
            "session_date": "2024-01-21",
            "status": "complete",
            "object_path": object_path,
            "url": url,
            "sas_url": url,
            "row_count": 24,
            "byte_size": 6789,
            "checksum": "crypto-checksum",
        })
    );
    assert_eq!(issuer.paths(), vec![object_path]);
}

#[tokio::test]
async fn raw_object_requires_authentication_before_sas_issuance() {
    let catalog = Arc::new(MemoryCatalogRepository::from_records([raw_record(None)]));
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(catalog);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let (status, headers, body) = send(
        &router,
        get(&raw_path(&canonical_path(
            date(1, 18),
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
        ))),
    )
    .await;

    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(headers["www-authenticate"], "Bearer");
    assert_eq!(body["error"]["code"], "unauthorized");
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn unknown_catalog_paths_do_not_receive_sas_credentials() {
    let catalog = Arc::new(MemoryCatalogRepository::from_records([raw_record(None)]));
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(catalog);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let (status, _, body) = send(
        &router,
        get_auth(&raw_path(&canonical_path(
            date(1, 18),
            AssetClass::Equity,
            DataType::Trade,
            "MSFT",
        ))),
    )
    .await;

    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(
        body,
        json!({
            "error": {
                "code": "raw_object_not_found",
                "message": "the requested object is not available in the catalog",
            }
        })
    );
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn malformed_or_out_of_scope_paths_are_rejected_before_sas() {
    let catalog = Arc::new(MemoryCatalogRepository::from_records([raw_record(None)]));
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(catalog);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let paths = [
        "../outside.parquet",
        "/date=2024-01-18/asset_class=equity/data_type=trade/AAPL.parquet",
        r"date=2024-01-18\asset_class=equity\data_type=trade\AAPL.parquet",
        "date=2024-01-18/asset_class=equity/data_type=trade/../AAPL.parquet",
        "date=2024-02-30/asset_class=equity/data_type=trade/AAPL.parquet",
        "date=2024-01-18/asset_class=futures/data_type=trade/AAPL.parquet",
        "date=2024-01-18/asset_class=equity/data_type=bars/AAPL.parquet",
        "date=2024-01-18/asset_class=equity/data_type=trade/aapl.parquet",
        "date=2024-01-18/asset_class=equity/data_type=trade/%2E%2E.parquet",
        "date=2024-01-18/asset_class=equity/data_type=trade/A%2F..%2FB.parquet",
    ];
    for path in paths {
        let (status, _, body) = send(&router, get_auth(&raw_path(path))).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "path: {path}");
        assert_eq!(body["error"]["code"], "invalid_object_path", "path: {path}");
    }
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn the_catalog_row_must_match_the_requested_path() {
    let catalog = Arc::new(MemoryCatalogRepository::from_records([raw_record(Some(
        "../outside.parquet".to_string(),
    ))]));
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(catalog);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let (status, _, body) = send(
        &router,
        get_auth(&raw_path(&canonical_path(
            date(1, 18),
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
        ))),
    )
    .await;

    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["code"], "raw_object_not_found");
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn bars_raw_paths_carry_the_interval_and_reject_bad_shapes() {
    let session_date = date(1, 18);
    let object_path = bars_path(session_date, "1min", "AAPL");
    let record = CoverageRecord::new(
        AssetClass::Equity,
        DataType::Bars,
        "AAPL",
        session_date,
        CoverageStatus::Complete,
        Some(object_path.clone()),
        7,
        890,
        Some("bars-checksum".to_string()),
        None,
        Some("1min".to_string()),
    )
    .expect("valid bars record");
    let catalog = Arc::new(MemoryCatalogRepository::from_records([record]));
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(catalog);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let (status, _, body) = send(&router, get_auth(&raw_path(&object_path))).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["data_type"], "bars");
    assert_eq!(body["object_path"], object_path);
    assert_eq!(issuer.paths(), vec![object_path]);

    let day = bars_path(session_date, "1d", "AAPL");
    let (status, _, body) = send(&router, get_auth(&raw_path(&day))).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["code"], "raw_object_not_found");

    for path in [
        "date=2024-01-18/asset_class=equity/data_type=bars/AAPL.parquet",
        "date=2024-01-18/asset_class=equity/data_type=bars/interval=5m/AAPL.parquet",
        "date=2024-01-18/asset_class=equity/data_type=bars/interval=1min/interval=1d/AAPL.parquet",
    ] {
        let (status, _, body) = send(&router, get_auth(&raw_path(path))).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "path: {path}");
        assert_eq!(body["error"]["code"], "invalid_object_path", "path: {path}");
    }
}

#[tokio::test]
async fn raw_object_reports_missing_sas_configuration_without_ensuring() {
    let catalog = Arc::new(MemoryCatalogRepository::from_records([raw_record(None)]));
    let callback = RecordingCallback::default();
    let mut state = base_state(catalog);
    state.sas_issuer = None;
    state.ensure_callback = Some(callback.callback());
    let router = build_app(state);

    let (status, _, body) = send(
        &router,
        get_auth(&raw_path(&canonical_path(
            date(1, 18),
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
        ))),
    )
    .await;

    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        body,
        json!({
            "error": {
                "code": "sas_unavailable",
                "message": "raw object SAS issuance is not configured",
            }
        })
    );
    assert!(callback.calls().is_empty());
}
