//! The ensure route's commits must be visible to the coverage route.

mod support;

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::response::Response;
use http_body_util::BodyExt;
use lake_ingestion::{CryptoDataProvider, EquityDataProvider};
use lake_local::{create_local_app_with, LocalLayout, LocalProviders};
use serde_json::{json, Value};
use tower::ServiceExt;

use support::{credentials, instant, FakeAlpaca, FakeCrypto};

fn app_environ() -> BTreeMap<String, String> {
    let mut environ = credentials();
    environ.insert(
        "LAKE_API__BEARER_TOKEN".to_string(),
        "local-token".to_string(),
    );
    // Accept the ensure before the launcher finishes so the caller must
    // observe the commit through the coverage route, exactly like the live
    // caller polling after a 202.
    environ.insert(
        "LAKE_LOCAL_ENSURE_DEADLINE_SECONDS".to_string(),
        "0.01".to_string(),
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

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ensure_commits_become_visible_to_the_coverage_route() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = app_environ();
    let equity: Arc<dyn EquityDataProvider> =
        Arc::new(FakeAlpaca::new().slow(Duration::from_millis(400)));
    let crypto: Arc<dyn CryptoDataProvider> = Arc::new(FakeCrypto::new());
    let router = create_local_app_with(
        &layout,
        &environ,
        Arc::new(|| instant(2026, 9, 23, 0, 0)),
        LocalProviders {
            equity: Some(equity),
            crypto: Some(crypto),
        },
    )
    .expect("local app");

    let payload = json!({
        "asset_class": "equity",
        "data_type": "quote",
        "symbol": "SPY",
        "start_date": "2026-09-21",
        "end_date": "2026-09-21",
    });
    let ensure = router
        .clone()
        .oneshot(
            Request::post("/v1/catalog/ensure")
                .header("authorization", "Bearer local-token")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&payload).expect("payload")))
                .expect("request"),
        )
        .await
        .expect("ensure");
    assert_eq!(ensure.status(), StatusCode::ACCEPTED);
    let accepted = body_json(ensure).await;
    assert!(accepted.get("job_id").is_some(), "{accepted:?}");

    let url = "/v1/catalog/coverage?asset_class=equity&data_type=quote&symbol=SPY&start_date=2026-09-21&end_date=2026-09-21";
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    let last = loop {
        tokio::time::sleep(Duration::from_millis(100)).await;
        let response = router
            .clone()
            .oneshot(
                Request::get(url)
                    .header("authorization", "Bearer local-token")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("coverage");
        assert_eq!(response.status(), StatusCode::OK);
        let body = body_json(response).await;
        let status = body["entries"][0]["status"].as_str().expect("entry");
        if status == "complete" || status == "empty" {
            break body;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "coverage never became terminal; last body {body:?}"
        );
    };
    assert_eq!(last["entries"][0]["status"], "complete");
}
