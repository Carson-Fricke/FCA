//! File-query and deferred SQL-query route behavior.

mod support;

use std::sync::Arc;

use axum::http::StatusCode;
use lake_catalog::{CoverageRecord, MemoryCatalogRepository, MemoryContractRepository};
use lake_contracts::{partition_path, AssetClass, DataType, Date};
use lake_query::{BoundedDuckDbQuery, QueryLimits};
use serde_json::json;
use support::*;

fn date(month: u32, day: u32) -> Date {
    Date::from_ymd(2024, month, day).expect("valid date")
}

fn file_request() -> serde_json::Value {
    json!({
        "mode": "file",
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "aapl",
        "start_date": "2024-01-18",
        "end_date": "2024-01-19",
        "ensure": false,
    })
}

fn complete_equity(session_date: Date) -> CoverageRecord {
    complete_record(AssetClass::Equity, DataType::Trade, "AAPL", session_date)
}

fn complete_bars(interval: &str, session_date: Date) -> CoverageRecord {
    let object_path = partition_path(
        session_date,
        AssetClass::Equity,
        DataType::Bars,
        Some(interval),
        "AAPL",
    )
    .expect("canonical bars path");
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Bars,
        "AAPL",
        session_date,
        lake_catalog::CoverageStatus::Complete,
        Some(object_path),
        12,
        3456,
        Some("checksum".to_string()),
        None,
        Some(interval.to_string()),
    )
    .expect("valid bars record")
}

#[tokio::test]
async fn bars_file_query_threads_the_interval_through_the_response() {
    let catalog = shared_catalog([complete_bars("1min", date(1, 18))]);
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let request = json!({
        "mode": "file",
        "asset_class": "equity",
        "data_type": "bars",
        "symbol": "aapl",
        "interval": "1min",
        "start_date": "2024-01-18",
        "end_date": "2024-01-18",
        "ensure": false,
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    let object_path =
        "date=2024-01-18/asset_class=equity/data_type=bars/interval=1min/AAPL.parquet";
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["files"][0]["data_type"], "bars");
    assert_eq!(body["files"][0]["interval"], "1min");
    assert_eq!(body["files"][0]["object_path"], object_path);
    assert_eq!(issuer.paths(), vec![object_path.to_string()]);

    let day = json!({
        "mode": "file",
        "asset_class": "equity",
        "data_type": "bars",
        "symbol": "aapl",
        "interval": "1d",
        "start_date": "2024-01-18",
        "end_date": "2024-01-18",
        "ensure": false,
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), day)).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "coverage_incomplete");
    assert_eq!(issuer.paths().len(), 1);
}

#[tokio::test]
async fn sql_query_over_bars_registers_the_interval_partition() {
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
    let catalog = shared_catalog([complete_bars("1min", date(1, 18))]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.bounded_query = Some(Arc::new(BoundedDuckDbQuery::new(
        temporary.path(),
        QueryLimits::new(1_000_000, 500_000).expect("limits"),
    )));
    let router = build_app(state);

    let request = json!({
        "mode": "sql",
        "asset_class": "equity",
        "data_type": "bars",
        "symbol": "aapl",
        "interval": "1min",
        "sql": "SELECT symbol, count(*) AS n FROM data GROUP BY symbol",
        "start_date": "2024-01-18",
        "end_date": "2024-01-18",
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["rows"], json!([{"symbol": "AAPL", "n": 2}]));
}

#[tokio::test]
async fn complete_file_query_returns_files_and_catalog_metadata() {
    let catalog = shared_catalog([complete_equity(date(1, 18))]);
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let mut request = file_request();
    request["end_date"] = json!("2024-01-18");
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    let object_path = "date=2024-01-18/asset_class=equity/data_type=trade/AAPL.parquet";
    let url = format!("https://blob.test/{object_path}?read-only-expiring");
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body,
        json!({
            "files": [{
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "session_date": "2024-01-18",
                "object_path": object_path,
                "url": url,
                "sas_url": url,
                "row_count": 12,
                "byte_size": 3456,
                "checksum": "checksum",
            }],
            "results": {
                "requested_sessions": 1,
                "returned_files": 1,
                "complete_sessions": 1,
                "pending_sessions": 0,
                "state": "complete",
            },
            "limitations": [
                "Only complete catalog partitions are returned.",
                "URLs are read-only, object-scoped, and expire according to SAS policy.",
            ],
        })
    );
    assert_eq!(issuer.paths(), vec![object_path.to_string()]);
}

#[tokio::test]
async fn incomplete_file_query_without_ensure_is_a_coverage_rejection() {
    let catalog = shared_catalog([
        complete_equity(date(1, 18)),
        status_record(
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
            date(1, 19),
            lake_catalog::CoverageStatus::Empty,
        ),
    ]);
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let (status, _, body) =
        send(&router, post_json("/v1/query", Some(TOKEN), file_request())).await;

    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        body,
        json!({
            "error": {
                "code": "coverage_incomplete",
                "message": "requested coverage is incomplete; set ensure=true to request coverage",
            }
        })
    );
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn issuer_failure_is_reported_as_sas_unavailable() {
    let catalog = shared_catalog([complete_equity(date(1, 18))]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::new(FailingIssuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let mut request = file_request();
    request["end_date"] = json!("2024-01-18");
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "sas_unavailable");
    assert!(body["error"]["message"]
        .as_str()
        .expect("message")
        .starts_with("file query SAS issuance failed"));
}

#[tokio::test]
async fn ensure_pending_file_query_returns_202_with_the_ensured_files() {
    let catalog = shared_catalog([complete_equity(date(1, 18))]);
    let issuer = Arc::new(RecordingIssuer::default());
    let callback = RecordingCallback::default();
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    state.ensure_callback = Some(callback.callback());
    let router = build_app(state);

    let mut request = file_request();
    request["ensure"] = json!(true);
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["results"]["pending_sessions"], 1);
    assert_eq!(body["results"]["state"], "ensure_pending");
    assert_eq!(body["files"].as_array().expect("files").len(), 1);
    assert_eq!(
        body["limitations"].as_array().expect("limitations").len(),
        3
    );
    assert_eq!(callback.calls().len(), 1);
    assert_eq!(issuer.paths().len(), 1);
}

#[tokio::test]
async fn requested_ensure_without_a_callback_is_unavailable() {
    let catalog = shared_catalog([]);
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let mut request = file_request();
    request["ensure"] = json!(true);
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "ensure_unavailable");
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn complete_file_query_without_an_issuer_is_unavailable() {
    let catalog = shared_catalog([complete_equity(date(1, 18))]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = None;
    let router = build_app(state);

    let mut request = file_request();
    request["end_date"] = json!("2024-01-18");
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "body: {body}");
    assert_eq!(body["error"]["code"], "sas_unavailable");
    assert_eq!(
        body["error"]["message"],
        "file query SAS issuance is not configured"
    );
}

#[tokio::test]
async fn ensure_callback_receives_the_request_identity() {
    let catalog = shared_catalog([complete_equity(date(1, 18))]);
    let issuer = Arc::new(RecordingIssuer::default());
    let callback = RecordingCallback::default();
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    state.ensure_callback = Some(callback.callback());
    let router = build_app(state);

    let mut request = file_request();
    request["ensure"] = json!(true);
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["results"]["state"], "ensure_pending");
    assert_eq!(
        callback.calls(),
        vec![(
            AssetClass::Equity,
            DataType::Trade,
            None,
            "aapl".to_string(),
            date(1, 18),
            date(1, 19),
        )]
    );
}

#[tokio::test]
async fn ensure_repairs_the_gap_through_the_injected_callback() {
    let catalog = shared_catalog([]);
    let issuer = Arc::new(RecordingIssuer::default());
    let callback = filling_callback(
        Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>,
        vec![complete_equity(date(1, 18)), complete_equity(date(1, 19))],
    );
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    state.ensure_callback = Some(callback);
    let router = build_app(state);

    let mut request = file_request();
    request["ensure"] = json!(true);
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["results"]["state"], "complete");
    assert_eq!(body["files"].as_array().expect("files").len(), 2);
    assert_eq!(issuer.paths().len(), 2);
}

#[tokio::test]
async fn crypto_file_query_returns_canonical_files_for_every_utc_day() {
    let session_dates = [date(1, 19), date(1, 20), date(1, 21)];
    let catalog = shared_catalog(session_dates.iter().map(|session_date| {
        complete_record(
            AssetClass::Crypto,
            DataType::Quote,
            "BTC/USD",
            *session_date,
        )
    }));
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let request = json!({
        "mode": "file",
        "asset_class": "crypto",
        "data_type": "quote",
        "symbol": "btc/usd",
        "start_date": "2024-01-19",
        "end_date": "2024-01-21",
        "ensure": false,
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    let expected_paths: Vec<String> = session_dates
        .iter()
        .map(|session_date| {
            partition_path(
                *session_date,
                AssetClass::Crypto,
                DataType::Quote,
                None,
                "BTC/USD",
            )
            .expect("path")
        })
        .collect();
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["results"]["state"], "complete");
    assert_eq!(
        body["files"]
            .as_array()
            .expect("files")
            .iter()
            .map(|file| file["object_path"].clone())
            .collect::<Vec<_>>(),
        expected_paths
            .iter()
            .map(|path| json!(path))
            .collect::<Vec<_>>()
    );
    assert!(expected_paths
        .iter()
        .all(|path| path.ends_with("/BTC%2FUSD.parquet")));
    assert_eq!(issuer.paths(), expected_paths);
}

#[tokio::test]
async fn crypto_file_query_treats_the_open_utc_day_as_partial() {
    let catalog = shared_catalog([complete_record(
        AssetClass::Crypto,
        DataType::Quote,
        "BTC/USD",
        date(1, 22),
    )]);
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let request = json!({
        "mode": "file",
        "asset_class": "crypto",
        "data_type": "quote",
        "symbol": "BTC/USD",
        "start_date": "2024-01-22",
        "end_date": "2024-01-22",
        "ensure": false,
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        body,
        json!({
            "error": {
                "code": "coverage_incomplete",
                "message": "requested coverage is incomplete; set ensure=true to request coverage",
            }
        })
    );
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn option_file_query_returns_the_selected_osi_partition() {
    let contract_symbol = "SPY240119C00500000";
    let catalog = shared_catalog([complete_record(
        AssetClass::Option,
        DataType::Trade,
        contract_symbol,
        date(1, 19),
    )]);
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract(contract_symbol, "SPY", date(1, 19)),
    ])));
    let router = build_app(state);

    let request = json!({
        "mode": "file",
        "asset_class": "option",
        "data_type": "trade",
        "underlying": "SPY",
        "contract_symbols": [contract_symbol],
        "start_date": "2024-01-19",
        "end_date": "2024-01-19",
        "ensure": false,
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["files"][0]["asset_class"], "option");
    assert_eq!(body["files"][0]["symbol"], contract_symbol);
    assert_eq!(
        issuer.paths(),
        vec![
            "date=2024-01-19/asset_class=option/data_type=trade/SPY240119C00500000.parquet"
                .to_string()
        ]
    );
}

#[tokio::test]
async fn option_query_selection_errors_keep_the_python_codes() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let request = json!({
        "mode": "file",
        "asset_class": "option",
        "data_type": "trade",
        "underlying": "SPY",
        "start_date": "2024-01-19",
        "end_date": "2024-01-19",
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "option_selection_unavailable");
    assert_eq!(
        body["error"]["message"],
        "option contract metadata or a persisted front set is required"
    );
}

#[tokio::test]
async fn option_sql_query_is_unavailable_without_an_engine() {
    let catalog = shared_catalog([complete_record(
        AssetClass::Option,
        DataType::Trade,
        "SPY240119C00500000",
        date(1, 19),
    )]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract("SPY240119C00500000", "SPY", date(1, 19)),
    ])));
    let router = build_app(state);

    let request = json!({
        "mode": "sql",
        "asset_class": "option",
        "data_type": "trade",
        "underlying": "SPY",
        "contract_symbols": ["SPY240119C00500000"],
        "start_date": "2024-01-19",
        "end_date": "2024-01-19",
        "sql": "SELECT 1",
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "query_unavailable");
    assert_eq!(
        body["error"]["message"],
        "bounded SQL query storage is not configured"
    );
}

#[tokio::test]
async fn sql_mode_without_an_engine_returns_query_unavailable() {
    let catalog = shared_catalog([complete_equity(date(1, 18))]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = None;
    let router = build_app(state);

    let request = json!({
        "mode": "sql",
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "AAPL",
        "start_date": "2024-01-18",
        "end_date": "2024-01-18",
        "sql": "SELECT * FROM data",
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        body,
        json!({
            "error": {
                "code": "query_unavailable",
                "message": "bounded SQL query storage is not configured",
            }
        })
    );
}

#[tokio::test]
async fn sql_mode_over_incomplete_coverage_is_rejected_before_the_engine() {
    let catalog = shared_catalog([]);
    let router = build_app(base_state(
        Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>
    ));

    let request = json!({
        "mode": "sql",
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "AAPL",
        "start_date": "2024-01-18",
        "end_date": "2024-01-18",
        "sql": "SELECT * FROM data",
    });
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "coverage_incomplete");
}

#[tokio::test]
async fn malformed_complete_catalog_paths_are_incomplete() {
    let mut record = complete_equity(date(1, 18));
    record.object_path = Some("../outside.parquet".to_string());
    let catalog = shared_catalog([record]);
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.sas_issuer = Some(Arc::clone(&issuer) as Arc<dyn lake_query::FileUrlIssuer>);
    let router = build_app(state);

    let mut request = file_request();
    request["end_date"] = json!("2024-01-18");
    let (status, _, body) = send(&router, post_json("/v1/query", Some(TOKEN), request)).await;

    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "coverage_incomplete");
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn query_requires_authentication_and_rejects_mismatched_fields() {
    let catalog = shared_catalog([complete_equity(date(1, 18))]);
    let router = build_app(base_state(
        Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>
    ));

    let (unauthenticated, _, _) = send(&router, post_json("/v1/query", None, file_request())).await;
    let mut with_sql = file_request();
    with_sql["sql"] = json!("select * from data");
    let (mismatched, _, mismatched_body) =
        send(&router, post_json("/v1/query", Some(TOKEN), with_sql)).await;
    let mut missing_mode = file_request();
    missing_mode.as_object_mut().expect("object").remove("mode");
    let (missing, _, missing_body) =
        send(&router, post_json("/v1/query", Some(TOKEN), missing_mode)).await;

    assert_eq!(unauthenticated, StatusCode::UNAUTHORIZED);
    assert_eq!(mismatched, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(mismatched_body["error"]["code"], "validation_error");
    assert_eq!(missing, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(missing_body["error"]["code"], "validation_error");
}

#[tokio::test]
async fn query_rejects_a_body_without_a_json_content_type() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let request = axum::http::Request::builder()
        .method("POST")
        .uri("/v1/query")
        .header(axum::http::header::AUTHORIZATION, format!("Bearer {TOKEN}"))
        .body(axum::body::Body::from(file_request().to_string()))
        .expect("request");
    let (status, _, body) = send(&router, request).await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "validation_error");
}
