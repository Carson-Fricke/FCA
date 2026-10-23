//! Bearer authentication for the harness HTTP boundary.
//!
//! Mirrors `harness.http.application.configured_bearer_token` and the lake
//! bearer authenticator: a static token is compared in constant time and a
//! missing or non-bearer credential fails closed with a uniform 401 shape.

use std::fmt;

use axum::body::Body;
use axum::extract::State;
use axum::http::header;
use axum::http::Request;
use axum::http::StatusCode;
use axum::middleware::Next;
use axum::response::IntoResponse;
use axum::response::Response;

use super::models::error_body;
use super::state::AppState;

/// Environment variable consulted when no explicit token is configured.
pub const BEARER_TOKEN_VARIABLE: &str = "HARNESS_API__BEARER_TOKEN";

/// Failures raised while resolving the configured bearer token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuthError {
    /// The configured token was present but blank.
    EmptyToken,
}

impl fmt::Display for AuthError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyToken => formatter.write_str("bearer token must be a non-empty string"),
        }
    }
}

impl std::error::Error for AuthError {}

/// Resolve the explicit token or the runtime-configured static token.
///
/// # Errors
///
/// Returns [`AuthError::EmptyToken`] when a configured token is blank.
pub fn configured_bearer_token(bearer_token: Option<String>) -> Result<Option<String>, AuthError> {
    let token = match bearer_token {
        Some(token) => Some(token),
        None => std::env::var(BEARER_TOKEN_VARIABLE).ok(),
    };
    if token
        .as_deref()
        .is_some_and(|value| value.trim().is_empty())
    {
        return Err(AuthError::EmptyToken);
    }
    Ok(token)
}

fn constant_time_eq(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    let mut difference = left.len() ^ right.len();
    let length = left.len().max(right.len());
    for index in 0..length {
        let a = left.get(index).copied().unwrap_or(0);
        let b = right.get(index).copied().unwrap_or(0);
        difference |= usize::from(a ^ b);
    }
    difference == 0
}

fn unauthorized() -> Response {
    let mut response = (
        StatusCode::UNAUTHORIZED,
        axum::Json(error_body(
            "unauthorized",
            "a valid bearer token is required",
            None,
        )),
    )
        .into_response();
    response.headers_mut().insert(
        header::WWW_AUTHENTICATE,
        header::HeaderValue::from_static("Bearer"),
    );
    response
}

/// Validate the presented bearer credential against the configured token.
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
    let credential = credential.trim();
    match expected_token {
        Some(expected) => constant_time_eq(credential, expected),
        None => false,
    }
}

/// Axum middleware requiring a valid bearer credential on protected routes.
pub async fn require_bearer(
    State(state): State<AppState>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let header_value = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    if authenticate_bearer(header_value, state.bearer_token.as_deref()) {
        next.run(request).await
    } else {
        unauthorized()
    }
}
