//! Authenticated backtest launch and job polling routes.

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::json;

use crate::http::launcher::{mark_failed, LaunchJob};
use crate::http::models::{ApiError, BacktestJobStatusResponse, LaunchAcceptedResponse};
use crate::http::state::AppState;
use crate::http::store::{JobRecord, JobState};

/// Build the launch and polling routes without binding them to a queue.
pub fn launch_router() -> Router<AppState> {
    Router::new()
        .route("/v1/backtests/jobs", post(launch_job))
        .route("/v1/backtests/jobs/{job_id}", get(job_status))
}

async fn launch_job(State(state): State<AppState>, body: Bytes) -> Result<Response, ApiError> {
    let configuration = state.configuration_parser.parse(&body).map_err(|error| {
        ApiError::validation_error("the submitted document is not a valid backtest configuration")
            .with_details(json!(error.to_string()))
    })?;
    let job_id = configuration.run_id.clone();
    let now = state.clock.now();
    state
        .job_repository
        .upsert(JobRecord::new(&job_id, JobState::Queued, now, now));

    let job = LaunchJob {
        job_id: job_id.clone(),
        configuration,
    };
    if let Err(error) = state.launcher.launch(&job) {
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
    let location = HeaderValue::from_str(&format!("/v1/backtests/jobs/{job_id}"))
        .map_err(|_| ApiError::validation_error("job_id is not a valid header value"))?;
    let mut http = (StatusCode::ACCEPTED, Json(response)).into_response();
    http.headers_mut().insert(header::LOCATION, location);
    Ok(http)
}

async fn job_status(
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
