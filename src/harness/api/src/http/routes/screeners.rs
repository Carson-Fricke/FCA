//! Authenticated screening launch route.
//!
//! `POST /v1/screeners/jobs` mirrors the study launch route but hands the work
//! to the injected [`ScreenLauncher`](crate::http::screener::ScreenLauncher),
//! which runs the job on a detached worker; the handler returns as soon as the
//! job is admitted. Status, artifacts, and deletion live on the neutral
//! `/v1/jobs/{job_id}` routes.

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use serde_json::json;

use crate::http::launcher::mark_failed;
use crate::http::models::{ApiError, LaunchAcceptedResponse};
use crate::http::screener::ScreenLaunchJob;
use crate::http::state::AppState;
use crate::http::store::{Admission, JobKind, JobRecord, StoreError};

/// Build the screening launch route.
pub fn screeners_router() -> Router<AppState> {
    Router::new().route("/v1/screeners/jobs", post(launch_screener))
}

async fn launch_screener(State(state): State<AppState>, body: Bytes) -> Result<Response, ApiError> {
    super::super::blocking::run(state.control.clone(), move || {
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
        let job_id = configuration.job_id.clone();
        let now = state.clock.now();
        let mut record =
            JobRecord::new(&job_id, JobKind::Screen, screen.configuration_digest(), now);
        record.strategy_id = screen.definition_id.clone();
        record.data_start =
            chrono::NaiveDate::parse_from_str(&screen.window_start(), "%Y-%m-%d").ok();
        record.data_end = chrono::NaiveDate::parse_from_str(&screen.window_end(), "%Y-%m-%d").ok();
        record.configuration_digest = Some(screen.configuration_digest());
        record.accepted_input = screen.submission_document();
        match state.job_store.admit(record) {
            Err(StoreError::Conflict(detail)) => return Err(ApiError::job_conflict(&detail)),
            Err(error) => return Err(ApiError::store_error(&error.to_string())),
            Ok(Admission::Joined(existing)) => return accepted(&existing),
            Ok(Admission::Created(_)) => {}
        }

        let job = ScreenLaunchJob {
            job_id: job_id.clone(),
            configuration: screen,
        };
        if let Err(error) = state.screener_launcher.launch(&job) {
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
    })
    .await?
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
