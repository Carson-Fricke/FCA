//! Axum HTTP boundary for the quant-harness API.
//!
//! This module ports `harness.http` onto axum. It mirrors the Python route
//! paths, payloads, response models, bearer authentication, and provenance
//! fields (`run_digest`, `configuration_digest`) while staying self-contained:
//! the results store, job repository, clock, configuration parser, and
//! backtest runner are all injected seams, so the HTTP surface is exercised
//! without the Python engine.

pub mod auth;
pub mod cli;
pub mod clock;
pub mod configuration;
pub mod launcher;
pub mod models;
pub mod routes;
pub mod server;
pub mod state;
pub mod store;

use axum::routing::get;
use axum::{middleware, Router};

use auth::AuthError;
use state::AppConfig;

/// Build the API application with an injected, testable authentication layer.
///
/// # Errors
///
/// Returns [`AuthError`] when a configured bearer token is blank.
pub fn create_app(config: AppConfig) -> Result<Router, AuthError> {
    let bearer_token = auth::configured_bearer_token(config.bearer_token.clone())?;
    let state = config.into_state(bearer_token);

    let public: Router<state::AppState> = Router::new()
        .route("/health", get(routes::health))
        .route("/healthz", get(routes::health));

    let protected = routes::runs_router()
        .merge(routes::launch_router())
        .route("/v1/auth/check", get(routes::auth_check))
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            auth::require_bearer,
        ));

    Ok(public.merge(protected).with_state(state))
}
