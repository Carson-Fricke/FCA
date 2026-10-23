//! Health and credential-check routes.

use axum::Json;

use crate::http::models::AuthCheckResponse;
use crate::http::models::HealthResponse;

/// Probe-safe liveness response; requires no credentials.
pub async fn health() -> Json<HealthResponse> {
    Json(HealthResponse { status: "ok" })
}

/// Minimal protected route for credential checks.
pub async fn auth_check() -> Json<AuthCheckResponse> {
    Json(AuthCheckResponse { status: "ok" })
}
