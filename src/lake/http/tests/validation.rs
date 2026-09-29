//! FastAPI-style validation failures across the published routes.

mod support;

use std::sync::Arc;

use axum::http::StatusCode;
use lake_catalog::MemoryCatalogRepository;
use serde_json::json;
use support::*;

fn coverage_url(query: &str) -> String {
    format!("/v1/catalog/coverage?{query}")
}

async fn validation_code(router: &axum::Router, path: &str) -> String {
    let (status, _, body) = send(router, get_auth(path)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "path: {path}");
    body["error"]["code"]
        .as_str()
        .unwrap_or_default()
        .to_string()
}

#[tokio::test]
async fn coverage_rejects_bad_enums_dates_and_symbols() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    for query in [
        "asset_class=futures&data_type=trade&symbol=AAPL&start_date=2024-01-01&end_date=2024-01-01",
        "asset_class=equity&data_type=bars&symbol=AAPL&start_date=2024-01-01&end_date=2024-01-01",
        "asset_class=equity&data_type=bars&symbol=AAPL&interval=5m&start_date=2024-01-01&end_date=2024-01-01",
        "asset_class=equity&data_type=trade&symbol=AAPL&interval=1min&start_date=2024-01-01&end_date=2024-01-01",
        "asset_class=option&data_type=bars&underlying=SPY&interval=1d&start_date=2024-01-01&end_date=2024-01-01",
        "asset_class=equity&data_type=trade&symbol=AAPL&start_date=not-a-date&end_date=2024-01-01",
        "asset_class=equity&data_type=trade&symbol=../AAPL&start_date=2024-01-01&end_date=2024-01-01",
        "asset_class=equity&data_type=trade&symbol=AAPL&start_date=2024-01-02&end_date=2024-01-01",
        "asset_class=equity&data_type=trade&start_date=2024-01-01&end_date=2024-01-01",
        "asset_class=equity&data_type=trade&symbol=AAPL&underlying=SPY&start_date=2024-01-01&end_date=2024-01-01",
        "asset_class=equity&data_type=trade&symbol=AAPL&start_date=2024-01-01&end_date=2024-01-01&unexpected=value",
    ] {
        let code = validation_code(&router, &coverage_url(query)).await;
        assert_eq!(code, "validation_error", "query: {query}");
    }
}

#[tokio::test]
async fn coverage_rejects_a_reversed_range_with_the_python_code() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let code = validation_code(
        &router,
        &coverage_url(
            "asset_class=equity&data_type=trade&symbol=AAPL&start_date=2024-01-02&end_date=2024-01-01",
        ),
    )
    .await;

    assert_eq!(code, "validation_error");
}

#[tokio::test]
async fn ensure_rejects_malformed_bodies() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    for body in [
        json!({
            "asset_class": "equity",
            "data_type": "trade",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "asset_class": "futures",
            "data_type": "trade",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "../AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "AAPL",
            "start_date": "2024-01-02",
            "end_date": "2024-01-01",
        }),
        json!({
            "asset_class": "option",
            "data_type": "trade",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "asset_class": "option",
            "data_type": "trade",
            "contract_symbols": ["SPY240119C00500000"],
            "contracts": ["SPY240119C00500000"],
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "asset_class": "equity",
            "data_type": "bars",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "asset_class": "equity",
            "data_type": "bars",
            "symbol": "AAPL",
            "interval": "5m",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "AAPL",
            "interval": "1min",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "asset_class": "option",
            "data_type": "bars",
            "underlying": "SPY",
            "interval": "1d",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
    ] {
        let (status, _, response) =
            send(&router, post_json("/v1/catalog/ensure", Some(TOKEN), body)).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
        assert_eq!(response["error"]["code"], "validation_error");
        assert!(response["error"]["details"].is_array());
    }
}

#[tokio::test]
async fn query_rejects_malformed_selection_fields() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    for body in [
        json!({
            "mode": "file",
            "asset_class": "equity",
            "data_type": "trade",
            "underlying": "SPY",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "mode": "file",
            "asset_class": "option",
            "data_type": "trade",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "mode": "sql",
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "mode": "file",
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
            "ensure": "yes",
        }),
        json!({
            "mode": "file",
            "asset_class": "equity",
            "data_type": "bars",
            "symbol": "AAPL",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "mode": "file",
            "asset_class": "equity",
            "data_type": "bars",
            "symbol": "AAPL",
            "interval": "5m",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "mode": "file",
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "AAPL",
            "interval": "1min",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
        json!({
            "mode": "file",
            "asset_class": "option",
            "data_type": "bars",
            "underlying": "SPY",
            "interval": "1d",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        }),
    ] {
        let (status, _, response) = send(&router, post_json("/v1/query", Some(TOKEN), body)).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
        assert_eq!(response["error"]["code"], "validation_error");
    }
}

#[tokio::test]
async fn job_ids_are_bounded() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let long = "a".repeat(129);
    let (status, _, body) = send(&router, get_auth(&format!("/v1/jobs/{long}"))).await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "validation_error");
}

#[tokio::test]
async fn raw_paths_are_required_and_bounded() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (missing_status, _, missing) = send(&router, get_auth("/v1/raw")).await;
    let long = "a".repeat(513);
    let (long_status, _, long_body) =
        send(&router, get_auth(&format!("/v1/raw?object_path={long}"))).await;

    assert_eq!(missing_status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(missing["error"]["code"], "validation_error");
    assert_eq!(long_status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(long_body["error"]["code"], "validation_error");
}
