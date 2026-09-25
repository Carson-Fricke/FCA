//! Local HTTP application tests: health, auth, coverage, and file URLs.

mod support;

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::response::Response;
use http_body_util::BodyExt;
use lake_ingestion::EquityDataProvider;
use lake_local::{create_local_app, run_local_ingest, LocalIngestOptions, LocalLayout};
use serde_json::{json, Value};
use tower::ServiceExt;

use support::{credentials, day, instant, load_settings, load_universes, FakeAlpaca};

fn app_environ() -> BTreeMap<String, String> {
    let mut environ = credentials();
    environ.insert(
        "LAKE_API__BEARER_TOKEN".to_string(),
        "local-token".to_string(),
    );
    environ
}

async fn body_json(response: Response) -> Value {
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    serde_json::from_slice(&bytes).expect("json body")
}

#[tokio::test]
async fn local_app_serves_health_auth_and_coverage_over_local_state() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = app_environ();
    let router = create_local_app(&layout, &environ, Arc::new(|| instant(2024, 1, 22, 0, 0)))
        .expect("local app");
    assert!(layout.is_initialized());
    assert!(layout.marker_path().is_file());

    let response = router
        .clone()
        .oneshot(
            Request::get("/health")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("health");
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_json(response).await, json!({ "status": "ok" }));

    let response = router
        .clone()
        .oneshot(
            Request::get("/v1/auth/check")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("unauthenticated auth check");
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

    let response = router
        .clone()
        .oneshot(
            Request::get("/v1/auth/check")
                .header("authorization", "Bearer local-token")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("authenticated auth check");
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(body_json(response).await, json!({ "status": "ok" }));

    let coverage = "/v1/catalog/coverage?asset_class=equity&data_type=trade&symbol=AAPL&start_date=2024-01-19&end_date=2024-01-19";
    let response = router
        .clone()
        .oneshot(
            Request::get(coverage)
                .header("authorization", "Bearer local-token")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("coverage");
    assert_eq!(response.status(), StatusCode::OK);
    let body = body_json(response).await;
    assert_eq!(body["entries"][0]["status"], "missing");
}

#[tokio::test]
async fn local_app_requires_a_bearer_token() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let error = create_local_app(
        &layout,
        &BTreeMap::new(),
        Arc::new(|| instant(2024, 1, 22, 0, 0)),
    )
    .expect_err("missing token");
    assert!(error.message.contains("LAKE_API__BEARER_TOKEN"));
}

#[tokio::test]
async fn local_app_issues_file_urls_for_complete_catalog_objects() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = app_environ();
    let provider = Arc::new(FakeAlpaca::new());
    let injected: Arc<dyn EquityDataProvider> = provider;
    let ingest = run_local_ingest(LocalIngestOptions {
        layout: layout.clone(),
        symbol: Some("AAPL".to_string()),
        session_date: Some(day(2024, 1, 19)),
        environ: environ.clone(),
        now: instant(2024, 1, 22, 0, 0),
        universes: load_universes(&environ),
        settings: load_settings(&environ),
        provider: Some(injected),
        catalog: None,
    })
    .await
    .expect("ingest");
    let router = create_local_app(&layout, &environ, Arc::new(|| instant(2024, 1, 22, 0, 0)))
        .expect("local app");

    let payload = json!({
        "mode": "file",
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "AAPL",
        "start_date": "2024-01-19",
        "end_date": "2024-01-19",
    });
    let response = router
        .clone()
        .oneshot(
            Request::post("/v1/query")
                .header("authorization", "Bearer local-token")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::to_vec(&payload).expect("request payload"),
                ))
                .expect("request"),
        )
        .await
        .expect("file query");
    assert_eq!(response.status(), StatusCode::OK);
    let body = body_json(response).await;
    assert_eq!(body["results"]["state"], "complete");
    assert_eq!(body["results"]["returned_files"], 1);
    assert_eq!(
        body["files"][0]["object_path"].as_str(),
        Some(ingest.objects[0].path.as_str())
    );
    assert!(body["files"][0]["url"]
        .as_str()
        .expect("url")
        .starts_with("file://"));
}
