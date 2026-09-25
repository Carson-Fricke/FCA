//! Bearer authentication for the market-lake HTTP boundary.
//!
//! Mirrors `lake.http.auth`: the static token is compared in constant time, a
//! missing or non-bearer credential fails closed, and every rejection renders
//! the uniform 401 envelope with `WWW-Authenticate: Bearer`. Entra JWT
//! validation is a later phase; this authenticator is structured so a JWT path
//! can extend it without changing the static one.

use axum::body::Body;
use axum::extract::State;
use axum::http::{header, Request};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};

use crate::error::ApiError;
use crate::state::ApiContext;

/// Compare two byte strings without early exit on content.
#[must_use]
pub fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    let mut difference = left.len() ^ right.len();
    let length = left.len().max(right.len());
    for index in 0..length {
        let a = left.get(index).copied().unwrap_or(0);
        let b = right.get(index).copied().unwrap_or(0);
        difference |= usize::from(a ^ b);
    }
    difference == 0
}

/// Validate one presented `Authorization` header against the configured token.
///
/// Returns whether the credential is acceptable. A missing header, a non-bearer
/// scheme, or an absent configured token always fails.
#[must_use]
pub fn authenticate_bearer(header_value: Option<&str>, expected_token: Option<&str>) -> bool {
    let Some(header_value) = header_value else {
        return false;
    };
    let Some((scheme, credential)) = header_value.split_once(' ') else {
        return false;
    };
    if !scheme.eq_ignore_ascii_case("bearer") {
        return false;
    }
    match expected_token {
        Some(expected) => constant_time_eq(credential.as_bytes(), expected.as_bytes()),
        None => false,
    }
}

/// Axum middleware requiring a valid bearer credential on protected routes.
pub(crate) async fn require_bearer(
    State(context): State<ApiContext>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let header_value = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    if authenticate_bearer(header_value, context.bearer_token.as_deref()) {
        next.run(request).await
    } else {
        ApiError::unauthorized().into_response()
    }
}
