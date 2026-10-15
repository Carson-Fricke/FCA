//! Remote MCP transport: expose the JSON-RPC MCP server over the axum boundary.
//!
//! A Thesis process (local or deployed) reaches one [`McpServer`] composed from
//! the same durable store as the HTTP API. `POST /mcp` carries one JSON-RPC
//! request and returns one JSON-RPC response; `GET /mcp/health` publishes the
//! server identity, capabilities, and tool catalog. Both routes sit behind the
//! shared bearer middleware, so an unauthenticated caller receives a uniform
//! 401 before any MCP dispatch happens.

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use mcp::auth::EnvironmentBearerTokenSource;
use mcp::server::{error_response, McpServer, PARSE_ERROR};
use mcp::tools::{Boundary, ContextError, HarnessToolsContext};
use serde_json::{json, Value};

use crate::local::{
    NativeBacktestSubmitter, NativeScreeningSubmitter, NativeStudySubmitter, NativeStudyValidator,
};

use super::models::error_body;
use super::native::NativeScreenRunner;
use super::state::AppState;
use super::store::JobStore;

/// Compose the MCP server the HTTP boundary serves.
///
/// The server runs in the `local` boundary and queries the same job store as
/// the REST routes, so a remote MCP call observes exactly the jobs the HTTP API
/// exposes.
///
/// # Errors
///
/// Returns [`ContextError`] when the local tool context cannot be composed.
pub fn compose_local_server(job_store: Arc<dyn JobStore>) -> Result<Arc<McpServer>, ContextError> {
    compose_server(job_store, None)
}
pub fn compose_server(
    job_store: Arc<dyn JobStore>,
    access: Option<Arc<dyn harness_lakeclient::MarketAccess>>,
) -> Result<Arc<McpServer>, ContextError> {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.backtest_submitter = Some(Arc::new(match &access {
        Some(access) => NativeBacktestSubmitter::with_access(job_store.clone(), access.clone()),
        None => NativeBacktestSubmitter::new(job_store.clone()),
    }));
    context.study_submitter = Some(Arc::new(match &access {
        Some(access) => NativeStudySubmitter::with_access(job_store.clone(), access.clone()),
        None => NativeStudySubmitter::new(job_store.clone()),
    }));
    context.screening_submitter = Some(Arc::new(NativeScreeningSubmitter::new(
        job_store.clone(),
        Arc::new(NativeScreenRunner {
            lake_client: access
                .map(harness_lakeclient::BlockingLakeClient::from_access)
                .transpose()
                .map_err(|e| ContextError(e.to_string()))?
                .map(Arc::new),
            ..Default::default()
        }),
    )));
    context.study_validator = Some(Arc::new(NativeStudyValidator));
    context.job_store = Some(job_store);
    let context = context.validate()?;
    Ok(Arc::new(mcp::create_server(context)?))
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(error_body(
            "mcp_unavailable",
            "the MCP server is not composed for this deployment",
            None,
        )),
    )
        .into_response()
}

fn parse_error(detail: &str) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(error_response(
            &Value::Null,
            PARSE_ERROR,
            "malformed JSON-RPC request body",
            Some(json!({"detail": detail})),
        )),
    )
        .into_response()
}

/// `POST /mcp`: dispatch one JSON-RPC request and return one response.
///
/// A malformed body answers `400` with a JSON-RPC parse error. A notification
/// (no `id`) answers `202 Accepted` with an empty body, matching the stdio
/// transport's no-output behavior.
pub async fn mcp_post(State(state): State<AppState>, body: Bytes) -> Response {
    let Some(server) = state.mcp_server.as_ref() else {
        return unavailable();
    };
    let text = match std::str::from_utf8(&body) {
        Ok(text) => text,
        Err(error) => return parse_error(&error.to_string()),
    };
    let request: Value = match serde_json::from_str(text) {
        Ok(request) => request,
        Err(error) => return parse_error(&error.to_string()),
    };
    match server.handle(&request) {
        Some(response) => Json(response).into_response(),
        None => StatusCode::ACCEPTED.into_response(),
    }
}

/// `GET /mcp/health`: publish the server identity, capabilities, and tools.
pub async fn mcp_health(State(state): State<AppState>) -> Response {
    let Some(server) = state.mcp_server.as_ref() else {
        return unavailable();
    };
    let mut body = server.describe();
    if let Some(object) = body.as_object_mut() {
        object.insert("status".to_string(), json!("ok"));
    }
    Json(body).into_response()
}
