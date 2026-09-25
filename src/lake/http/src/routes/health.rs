//! Probe-safe liveness and credential-check routes.

use crate::models::{AuthCheckResponse, HealthResponse};

/// Return a probe-safe liveness response without requiring credentials.
pub async fn health() -> axum::Json<HealthResponse> {
    axum::Json(HealthResponse { status: "ok" })
}

/// Return the minimal protected route confirming credentials were accepted.
pub async fn auth_check() -> axum::Json<AuthCheckResponse> {
    axum::Json(AuthCheckResponse { status: "ok" })
}
