//! Authenticated kind-specific job submission routes.

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use serde_json::json;

use crate::http::launcher::{mark_failed, LaunchJob};
use crate::http::models::{ApiError, LaunchAcceptedResponse};
use crate::http::state::AppState;
use crate::http::store::{Admission, JobKind, JobRecord, StoreError};

/// Build the submission routes without binding them to a queue.
pub fn launch_router() -> Router<AppState> {
    Router::new()
        .route("/v1/backtests/jobs", post(launch_backtest_job))
        .route("/v1/studies/jobs", post(launch_study_job))
}

async fn launch_backtest_job(
    State(state): State<AppState>,
    body: Bytes,
) -> Result<Response, ApiError> {
    super::super::blocking::run(state.control.clone(), move || {
        launch_with_shell(&state, &body, "backtest", JobKind::Backtest)
    })
    .await?
}

async fn launch_study_job(
    State(state): State<AppState>,
    body: Bytes,
) -> Result<Response, ApiError> {
    super::super::blocking::run(state.control.clone(), move || {
        launch_with_shell(&state, &body, "study", JobKind::Study)
    })
    .await?
}

fn launch_with_shell(
    state: &AppState,
    body: &[u8],
    expected_shell: &str,
    kind: JobKind,
) -> Result<Response, ApiError> {
    let configuration = state.configuration_parser.parse(body).map_err(|error| {
        ApiError::validation_error(&format!(
            "the submitted document is not a valid {expected_shell} configuration"
        ))
        .with_details(json!(error.to_string()))
    })?;
    let shell = configuration
        .document
        .get("shell")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    if shell != expected_shell {
        return Err(ApiError::validation_error(&format!(
            "the submitted document is not a valid {expected_shell} configuration"
        ))
        .with_details(json!(format!(
            "configuration shell must be '{expected_shell}', not '{shell}'"
        ))));
    }
    let job_id = configuration.job_id.clone();
    let now = state.clock.now();
    let digest =
        backtest::backtest::config::BacktestConfig::from_yaml(&configuration.document.to_string())
            .map_err(|e| ApiError::validation_error(&e.to_string()))?
            .configuration_hash();
    let mut record = JobRecord::new(&job_id, kind, &digest, now);
    record.strategy_id = configuration.strategy_ids.join(",");
    record.data_start = configuration.data_start;
    record.data_end = configuration.data_end;
    record.configuration_digest = Some(digest);
    record.accepted_input = configuration.document.clone();
    match state.job_store.admit(record) {
        Err(StoreError::Conflict(detail)) => return Err(ApiError::job_conflict(&detail)),
        Err(error) => return Err(ApiError::store_error(&error.to_string())),
        Ok(Admission::Joined(existing)) => return accepted(&existing),
        Ok(Admission::Created(_)) => {}
    }

    let job = LaunchJob {
        job_id: job_id.clone(),
        kind,
        configuration,
    };
    if let Err(error) = state.launcher.launch(&job) {
        mark_failed(
            state.job_store.as_ref(),
            &job_id,
            state.clock.as_ref(),
            &error,
        )
        .map_err(|error| ApiError::store_error(&error.to_string()))?;
    }

    let record = state
        .job_store
        .try_get(&job_id)
        .map_err(|error| ApiError::store_error(&error.to_string()))?
        .ok_or_else(ApiError::job_not_found)?;
    accepted(&record)
}

fn accepted(record: &JobRecord) -> Result<Response, ApiError> {
    let response = LaunchAcceptedResponse {
        job_id: record.job_id.clone(),
        state: record.state,
    };
    let location = HeaderValue::from_str(&format!("/v1/jobs/{}", record.job_id))
        .map_err(|_| ApiError::validation_error("job_id is not a valid header value"))?;
    let mut http = (StatusCode::ACCEPTED, Json(response)).into_response();
    http.headers_mut().insert(header::LOCATION, location);
    Ok(http)
}
