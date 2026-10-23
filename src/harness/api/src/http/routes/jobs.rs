//! Authenticated job query and artifact routes.

use std::collections::HashMap;

use axum::extract::Path;
use axum::extract::Query;
use axum::extract::State;
use axum::http::header;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::routing::get;
use axum::Json;
use axum::Router;
use serde_json::json;

use crate::http::models::ApiError;
use crate::http::models::ArtifactListResponse;
use crate::http::models::ArtifactSummary;
use crate::http::models::JobListResponse;
use crate::http::models::JobStatusResponse;
use crate::http::state::AppState;
use harness_execution::jobs::JobKind;
use harness_execution::jobs::JobState;

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
    super::super::blocking::run(state.control.clone(), move || {
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
        let jobs = state
            .queries
            .try_list(
                kind,
                state_filter,
                params.get("strategy_id").map(String::as_str),
            )
            .map_err(|error| ApiError::store_error(&error.to_string()))?;
        Ok(Json(JobListResponse { jobs }))
    })
    .await?
}

async fn get_job(
    State(state): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<Json<JobStatusResponse>, ApiError> {
    super::super::blocking::run(state.control.clone(), move || {
        identifier(&job_id)?;
        state
            .queries
            .try_get(&job_id)
            .map_err(|e| ApiError::store_error(&e.to_string()))?
            .map(Json)
            .ok_or_else(ApiError::job_not_found)
    })
    .await?
}

async fn delete_job(
    State(state): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<StatusCode, ApiError> {
    super::super::blocking::run(state.control.clone(), move || {
        identifier(&job_id)?;
        if state
            .retention
            .remove(&job_id)
            .map_err(|error| ApiError::store_error(&error.to_string()))?
        {
            Ok(StatusCode::NO_CONTENT)
        } else {
            Err(ApiError::job_not_found())
        }
    })
    .await?
}

async fn list_artifacts(
    State(state): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<Json<ArtifactListResponse>, ApiError> {
    super::super::blocking::run(state.control.clone(), move || {
        identifier(&job_id)?;
        let inventory = state
            .artifacts
            .artifacts(&job_id)
            .map_err(|e| ApiError::store_error(&e.to_string()))?
            .ok_or_else(ApiError::job_not_found)?;
        let artifacts = inventory
            .into_iter()
            .map(|info| ArtifactSummary {
                name: info.name,
                sas_url: state
                    .sas_issuer
                    .as_ref()
                    .map(|issuer| issuer.issue(&info.uri)),
                uri: info.uri,
                sha256: info.sha256,
                size_bytes: info.size_bytes,
            })
            .collect();
        Ok(Json(ArtifactListResponse { job_id, artifacts }))
    })
    .await?
}

async fn download_artifact(
    State(state): State<AppState>,
    Path((job_id, artifact_name)): Path<(String, String)>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Response, ApiError> {
    super::super::blocking::run(state.control.clone(), move || {
        identifier(&job_id)?;
        if artifact_name.is_empty() || artifact_name.len() > ARTIFACT_NAME_MAX {
            return Err(ApiError::artifact_not_found());
        }
        if state
            .queries
            .try_get(&job_id)
            .map_err(|error| ApiError::store_error(&error.to_string()))?
            .is_none()
        {
            return Err(ApiError::job_not_found());
        }
        if query
            .get("representation")
            .is_some_and(|value| value == "json")
        {
            let payload = state
                .artifacts
                .artifact(&job_id, &artifact_name)
                .map_err(|e| ApiError::store_error(&e.to_string()))?
                .ok_or_else(ApiError::artifact_not_found)?;
            let (encoding, content) = match std::str::from_utf8(&payload) {
                Ok(text) => ("utf-8", text.to_string()),
                Err(_) => ("hex", hex::encode(&payload)),
            };
            return Ok(Json(
                json!({"job_id":job_id,"name":artifact_name,"encoding":encoding,"content":content}),
            )
            .into_response());
        }
        let transfer = state.transfers.clone().try_acquire_owned().map_err(|_| {
            ApiError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "transfer_busy",
                "artifact transfer capacity is occupied",
            )
        })?;
        let mut reader = state
            .artifacts
            .open_artifact(&job_id, &artifact_name)
            .map_err(|e| ApiError::store_error(&e.to_string()))?
            .ok_or_else(ApiError::artifact_not_found)?;
        let (sender, receiver) = tokio::sync::mpsc::channel(2);
        std::thread::spawn(move || {
            let _transfer = transfer;
            use std::io::Read;
            loop {
                let mut bytes = vec![0; 64 * 1024];
                match reader.read(&mut bytes) {
                    Ok(0) => break,
                    Ok(n) => {
                        bytes.truncate(n);
                        if sender
                            .blocking_send(Ok::<_, std::io::Error>(bytes))
                            .is_err()
                        {
                            break;
                        }
                    }
                    Err(error) => {
                        let _ = sender.blocking_send(Err(error));
                        break;
                    }
                }
            }
        });
        let stream = futures_util::stream::unfold(receiver, |mut receiver| async move {
            receiver.recv().await.map(|value| (value, receiver))
        });
        Ok((
            [(header::CONTENT_TYPE, "application/octet-stream")],
            axum::body::Body::from_stream(stream),
        )
            .into_response())
    })
    .await?
}
