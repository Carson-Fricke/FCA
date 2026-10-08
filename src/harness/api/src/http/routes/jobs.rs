//! Authenticated job query and artifact routes.

use std::collections::HashMap;

use axum::extract::{Path, Query, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::json;

use crate::http::models::{
    ApiError, ArtifactListResponse, ArtifactSummary, JobListResponse, JobStatusResponse,
};
use crate::http::state::AppState;
use crate::http::store::{artifact_digest, artifact_prefix, JobKind, JobState};

const IDENTIFIER_MAX: usize = 128;
const ARTIFACT_NAME_MAX: usize = 512;

/// Build the job query routes without binding them to a store.
pub fn jobs_router() -> Router<AppState> {
    Router::new()
        .route("/v1/jobs", get(list_jobs))
        .route("/v1/jobs/{job_id}", get(get_job).delete(delete_job))
        .route("/v1/jobs/{job_id}/artifacts", get(list_artifacts))
        .route(
            "/v1/jobs/{job_id}/artifacts/{*artifact_name}",
            get(download_artifact),
        )
}

fn identifier(value: &str) -> Result<&str, ApiError> {
    if value.is_empty() || value.len() > IDENTIFIER_MAX {
        return Err(ApiError::validation_error("request validation failed")
            .with_details(json!({"msg": "identifier must be 1 to 128 characters"})));
    }
    Ok(value)
}

async fn list_jobs(
    State(state): State<AppState>,
    Query(params): Query<HashMap<String, String>>,
) -> Result<Json<JobListResponse>, ApiError> {
    let kind = match params.get("kind") {
        Some(value) => Some(JobKind::parse(value).map_err(|_| {
            ApiError::validation_error("request validation failed").with_details(json!({
                "loc": ["query", "kind"],
                "msg": "unknown job kind",
            }))
        })?),
        None => None,
    };
    let state_filter = match params.get("state") {
        Some(value) => Some(JobState::parse(value).map_err(|_| {
            ApiError::validation_error("request validation failed").with_details(json!({
                "loc": ["query", "state"],
                "msg": "unknown job state",
            }))
        })?),
        None => None,
    };
    if let Some(strategy_id) = params.get("strategy_id") {
        if strategy_id.len() > IDENTIFIER_MAX {
            return Err(ApiError::validation_error("request validation failed")
                .with_details(json!({"loc": ["query", "strategy_id"]})));
        }
    }
    let jobs = state.job_store.list(
        kind,
        state_filter,
        params.get("strategy_id").map(String::as_str),
    );
    Ok(Json(JobListResponse { jobs }))
}

async fn get_job(
    State(state): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<Json<JobStatusResponse>, ApiError> {
    identifier(&job_id)?;
    state
        .job_store
        .get(&job_id)
        .map(Json)
        .ok_or_else(ApiError::job_not_found)
}

async fn delete_job(
    State(state): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<StatusCode, ApiError> {
    identifier(&job_id)?;
    if state.job_store.delete(&job_id) {
        Ok(StatusCode::NO_CONTENT)
    } else {
        Err(ApiError::job_not_found())
    }
}

async fn list_artifacts(
    State(state): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<Json<ArtifactListResponse>, ApiError> {
    identifier(&job_id)?;
    let stored = state
        .job_store
        .fetch(&job_id)
        .ok_or_else(ApiError::job_not_found)?;
    let prefix = artifact_prefix(&stored.record.job_id).map_err(|_| ApiError::job_not_found())?;
    let artifacts = stored
        .artifacts
        .iter()
        .map(|(name, payload)| {
            let object_path = format!("{prefix}/{name}");
            let sas_url = state
                .sas_issuer
                .as_ref()
                .map(|issuer| issuer.issue(&object_path));
            ArtifactSummary {
                name: name.clone(),
                uri: object_path,
                sha256: artifact_digest(payload),
                size_bytes: payload.len() as u64,
                sas_url,
            }
        })
        .collect();
    Ok(Json(ArtifactListResponse {
        job_id: stored.record.job_id,
        artifacts,
    }))
}

async fn download_artifact(
    State(state): State<AppState>,
    Path((job_id, artifact_name)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    identifier(&job_id)?;
    if artifact_name.is_empty() || artifact_name.len() > ARTIFACT_NAME_MAX {
        return Err(ApiError::artifact_not_found());
    }
    let stored = state
        .job_store
        .fetch(&job_id)
        .ok_or_else(ApiError::job_not_found)?;
    let payload = stored
        .artifacts
        .get(&artifact_name)
        .ok_or_else(ApiError::artifact_not_found)?;
    Ok((
        [(header::CONTENT_TYPE, "application/octet-stream")],
        payload.clone(),
    )
        .into_response())
}
