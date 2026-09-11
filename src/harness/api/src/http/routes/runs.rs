//! Authenticated backtest run query routes.

use std::collections::HashMap;

use axum::extract::{Path, Query, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::json;

use crate::http::models::{ApiError, ArtifactListResponse, ArtifactSummary, RunListResponse};
use crate::http::state::AppState;
use crate::http::store::{run_prefix, RunCatalogRecord, RunStatus};

const IDENTIFIER_MAX: usize = 128;
const ARTIFACT_NAME_MAX: usize = 512;

/// Build the run query routes without binding them to a store.
pub fn runs_router() -> Router<AppState> {
    Router::new()
        .route("/v1/backtests/runs", get(list_runs))
        .route(
            "/v1/backtests/runs/{run_id}",
            get(get_run).delete(delete_run),
        )
        .route("/v1/backtests/runs/{run_id}/artifacts", get(list_artifacts))
        .route(
            "/v1/backtests/runs/{run_id}/artifacts/{*artifact_name}",
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

async fn list_runs(
    State(state): State<AppState>,
    Query(params): Query<HashMap<String, String>>,
) -> Result<Json<RunListResponse>, ApiError> {
    let status = match params.get("status") {
        Some(value) => Some(RunStatus::parse(value).map_err(|_| {
            ApiError::validation_error("request validation failed").with_details(json!({
                "loc": ["query", "status"],
                "msg": "unknown run status",
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
    let runs = state
        .run_store
        .list(status, params.get("strategy_id").map(String::as_str));
    Ok(Json(RunListResponse { runs }))
}

async fn get_run(
    State(state): State<AppState>,
    Path(run_id): Path<String>,
) -> Result<Json<RunCatalogRecord>, ApiError> {
    identifier(&run_id)?;
    state
        .run_store
        .get(&run_id)
        .map(Json)
        .ok_or_else(ApiError::run_not_found)
}

async fn delete_run(
    State(state): State<AppState>,
    Path(run_id): Path<String>,
) -> Result<StatusCode, ApiError> {
    identifier(&run_id)?;
    if state.run_store.delete(&run_id) {
        Ok(StatusCode::NO_CONTENT)
    } else {
        Err(ApiError::run_not_found())
    }
}

async fn list_artifacts(
    State(state): State<AppState>,
    Path(run_id): Path<String>,
) -> Result<Json<ArtifactListResponse>, ApiError> {
    identifier(&run_id)?;
    let stored = state
        .run_store
        .fetch(&run_id)
        .ok_or_else(ApiError::run_not_found)?;
    let prefix = run_prefix(&stored.record.run_id).map_err(|_| ApiError::run_not_found())?;
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
                size: payload.len() as u64,
                object_path,
                sas_url,
            }
        })
        .collect();
    Ok(Json(ArtifactListResponse {
        run_id: stored.record.run_id,
        artifacts,
    }))
}

async fn download_artifact(
    State(state): State<AppState>,
    Path((run_id, artifact_name)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    identifier(&run_id)?;
    if artifact_name.is_empty() || artifact_name.len() > ARTIFACT_NAME_MAX {
        return Err(ApiError::artifact_not_found());
    }
    let stored = state
        .run_store
        .fetch(&run_id)
        .ok_or_else(ApiError::run_not_found)?;
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
