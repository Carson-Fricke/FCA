//! Authenticated screening launch and job polling routes.
//!
//! `POST /v1/screeners/jobs` mirrors the study launch route but hands the work
//! to the injected [`ScreenLauncher`](crate::http::screener::ScreenLauncher),
//! which runs the job on a detached worker; the handler returns as soon as the
//! job is accepted. `GET /v1/screeners/jobs/{job_id}` returns the same job
//! record shape as the backtest and study status routes.

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::json;

use crate::http::launcher::mark_failed;
use crate::http::models::{ApiError, BacktestJobStatusResponse, LaunchAcceptedResponse};
use crate::http::screener::ScreenLaunchJob;
use crate::http::state::AppState;
use crate::http::store::{JobRecord, JobState};

/// Build the screening launch and polling routes.
pub fn screeners_router() -> Router<AppState> {
    Router::new()
        .route("/v1/screeners/jobs", post(launch_screener))
        .route("/v1/screeners/jobs/{job_id}", get(screener_status))
}

async fn launch_screener(State(state): State<AppState>, body: Bytes) -> Result<Response, ApiError> {
    let configuration = state.configuration_parser.parse(&body).map_err(|error| {
        ApiError::validation_error("the submitted document is not a valid screen configuration")
            .with_details(json!(error.to_string()))
    })?;
    let shell = configuration
        .document
        .get("shell")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    if shell != "screen" {
        return Err(ApiError::validation_error(
            "the submitted document is not a valid screen configuration",
        )
        .with_details(json!(format!(
            "configuration shell must be 'screen', not '{shell}'"
        ))));
    }
    let Some(screen) = configuration.screen.clone() else {
        return Err(ApiError::validation_error(
            "the submitted document is not a valid screen configuration",
        ));
    };
    let job_id = configuration.run_id.clone();
    let now = state.clock.now();
    state
        .job_repository
        .upsert(JobRecord::new(&job_id, JobState::Queued, now, now));

    let job = ScreenLaunchJob {
        job_id: job_id.clone(),
        configuration: screen,
    };
    if let Err(error) = state.screener_launcher.launch(&job) {
        mark_failed(
            state.job_repository.as_ref(),
            &job_id,
            state.clock.as_ref(),
            &error,
        );
    }

    let record = state
        .job_repository
        .get(&job_id)
        .unwrap_or_else(|| JobRecord::new(&job_id, JobState::Queued, now, now));
    let response = LaunchAcceptedResponse {
        job_id: record.job_id.clone(),
        state: record.state,
    };
    let location = HeaderValue::from_str(&format!("/v1/screeners/jobs/{job_id}"))
        .map_err(|_| ApiError::validation_error("job_id is not a valid header value"))?;
    let mut http = (StatusCode::ACCEPTED, Json(response)).into_response();
    http.headers_mut().insert(header::LOCATION, location);
    Ok(http)
}

async fn screener_status(
    State(state): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<Json<BacktestJobStatusResponse>, ApiError> {
    if job_id.is_empty() || job_id.len() > 128 {
        return Err(ApiError::validation_error("request validation failed"));
    }
    state
        .job_repository
        .get(&job_id)
        .map(Json)
        .ok_or_else(ApiError::job_not_found)
}
