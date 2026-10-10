//! Published API startup and authentication through the CLI composition.

use std::collections::BTreeMap;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

#[test]
fn configured_azure_storage_is_validated_instead_of_ignored() {
    let environment = BTreeMap::from([("LAKE_STORAGE_ACCOUNT_NAME".into(), "/".into())]);
    let result = lake_cli::build_api_router(&environment);
    assert!(
        result.is_err(),
        "invalid hosted storage must fail API startup"
    );
}

#[tokio::test]
async fn published_router_keeps_anonymous_requests_out() {
    let environment = BTreeMap::from([("LAKE_API__BEARER_TOKEN".into(), "test-bearer".into())]);
    let router = lake_cli::build_api_router(&environment).expect("published router");
    let response = router
        .oneshot(
            Request::builder()
                .uri("/v1/auth/check")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}
