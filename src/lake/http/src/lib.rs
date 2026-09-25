//! Axum boundary publishing the market-lake catalog, ensure, query, and raw routes.
//!
//! This crate is the Rust port of `lake.http`: the published HTTP contract the
//! harness and Thesis consume. Paths, methods, status codes, JSON field names,
//! bearer authentication, and error envelopes stay byte-compatible with the
//! Python application.
//!
//! Two seams are deliberately injected rather than constructed here: the
//! repositories and URL issuer live in [`LakeApiState`], and the request
//! boundary configuration lives in [`LakeApiConfig`]. The coverage clock is an
//! injected provider so tests can pin the as-of instant that classifies a
//! currently open session as partial.
//!
//! The bounded DuckDB SQL mode is a later phase: with no SQL engine configured,
//! `POST /v1/query` with `mode: "sql"` returns the Python
//! `query_unavailable` rejection.

pub mod auth;
pub mod models;
pub mod routes;
pub mod state;

mod error;
mod selection;
mod validate;

use std::sync::Arc;

use axum::routing::{get, post};
use axum::{middleware, Router};

pub use error::{error_body, ApiError};
pub use state::{ApiClock, LakeApiConfig, LakeApiState};

/// Build the lake API with injected repositories and request configuration.
///
/// The returned router serves `/health` and `/healthz` without credentials and
/// requires the configured bearer token on every other route.
pub fn create_app(state: Arc<LakeApiState>, config: LakeApiConfig) -> Router {
    let context = state::ApiContext::new(state, &config);

    let public: Router<state::ApiContext> = Router::new()
        .route("/health", get(routes::health::health))
        .route("/healthz", get(routes::health::health));

    let protected: Router<state::ApiContext> = Router::new()
        .route("/v1/auth/check", get(routes::health::auth_check))
        .route("/v1/catalog/coverage", get(routes::catalog::coverage))
        .route("/v1/catalog/ensure", post(routes::ensure::ensure))
        .route("/v1/jobs/{job_id}", get(routes::ensure::job_status))
        .route("/v1/query", post(routes::query::query))
        .route("/v1/raw", get(routes::raw::raw_object))
        .route_layer(middleware::from_fn_with_state(
            context.clone(),
            auth::require_bearer,
        ));

    public
        .merge(protected)
        .fallback(routes::fallback)
        .method_not_allowed_fallback(routes::method_not_allowed)
        .with_state(context)
}
