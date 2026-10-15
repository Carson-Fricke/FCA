//! Health, authentication, and unmatched-route behavior.

mod support;

use std::sync::Arc;

use axum::http::{header, StatusCode};
use lake_catalog::MemoryCatalogRepository;
use lake_http::auth::authenticate_bearer;
use serde_json::json;
use support::*;

#[tokio::test]
async fn health_is_unauthenticated_and_has_probe_alias() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    for path in ["/health", "/healthz"] {
        let (status, _, body) = send(&router, get(path)).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, json!({"status": "ok"}));
    }
}

#[tokio::test]
async fn protected_route_rejects_missing_or_invalid_credentials() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    for request in [
        get("/v1/auth/check"),
        bearer_get("Bearer wrong-secret"),
        bearer_get("Basic test-secret"),
    ] {
        let (status, headers, body) = send(&router, request).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        assert_eq!(headers["www-authenticate"], "Bearer");
        assert_eq!(
            body,
            json!({"error": {"code": "unauthorized", "message": "a valid bearer token is required"}})
        );
    }
}

#[tokio::test]
async fn protected_route_accepts_the_configured_bearer() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (status, _, body) = send(&router, get_auth("/v1/auth/check")).await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, json!({"status": "ok"}));
}

#[tokio::test]
async fn authenticator_fails_closed_without_a_configured_token() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let mut request = get_auth("/v1/auth/check");
    request.headers_mut().insert(
        header::AUTHORIZATION,
        header::HeaderValue::from_static("Bearer any-value"),
    );
    let (status, _, _) = send(&router, request).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);

    let mut state = base_state(Arc::new(MemoryCatalogRepository::new()));
    state.bearer_token = None;
    let router = build_app_with(state, lake_http::LakeApiConfig { bearer_token: None });
    let (status, _, _) = send(&router, get_auth("/v1/auth/check")).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

#[test]
fn bearer_credentials_are_compared_in_constant_time() {
    assert!(authenticate_bearer(
        Some("Bearer test-secret"),
        Some("test-secret")
    ));
    assert!(!authenticate_bearer(
        Some("Bearer wrong"),
        Some("test-secret")
    ));
    assert!(!authenticate_bearer(None, Some("test-secret")));
    assert!(!authenticate_bearer(Some("Bearer test-secret"), None));
}

#[tokio::test]
async fn unmatched_routes_use_the_error_envelope_without_auth() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (status, _, body) = send(&router, get("/v1/nothing/here")).await;

    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(
        body,
        json!({"error": {"code": "http_error", "message": "Not Found"}})
    );
}

#[tokio::test]
async fn method_mismatch_uses_the_error_envelope() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let request = post_json("/health", None, json!({}));
    let (status, _, body) = send(&router, request).await;

    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(body["error"]["code"], "http_error");
}

/// Build an authenticated GET with an explicit authorization header value.
fn bearer_get(header_value: &str) -> axum::http::Request<axum::body::Body> {
    axum::http::Request::builder()
        .method("GET")
        .uri("/v1/auth/check")
        .header(header::AUTHORIZATION, header_value)
        .body(axum::body::Body::empty())
        .expect("request")
}
