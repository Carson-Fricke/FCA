//! Request and response models for the harness HTTP boundary.
//!
//! Field names and shapes mirror `harness.http.models` and `lake.http.models`
//! so existing clients decode responses unchanged.

use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::Json;
use serde::Serialize;
use serde_json::json;
use serde_json::Value;

use harness_execution::jobs::JobRecord;

/// Probe-safe liveness response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HealthResponse {
    /// Always `"ok"`.
    pub status: &'static str,
}

/// Credential-check response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AuthCheckResponse {
    /// Always `"ok"`.
    pub status: &'static str,
}

/// Stored jobs sorted by job ID.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct JobListResponse {
    /// Authoritative job records.
    pub jobs: Vec<JobRecord>,
}

/// One stored run artifact in the Thesis reproducibility-manifest shape.
///
/// The manifest entry is consumed both by Thesis's
/// `thesis.artifacts.importer.HarnessManifest` (which requires `name`, `uri`,
/// `sha256`, and `sizeBytes`) and by the harness status surface.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ArtifactSummary {
    /// Artifact name relative to the job artifact prefix.
    pub name: String,
    /// Full object path (`jobs/{job_id}/{name}`) as a confined URI.
    pub uri: String,
    /// SHA-256 hex digest of the artifact payload.
    pub sha256: String,
    /// Payload size in bytes.
    #[serde(rename = "sizeBytes")]
    pub size_bytes: u64,
    /// Optional read-only SAS URL.
    pub sas_url: Option<String>,
}

/// The artifact manifest stored below one run's blob prefix.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ArtifactListResponse {
    /// Run identifier.
    pub job_id: String,
    /// Artifact manifest entries.
    pub artifacts: Vec<ArtifactSummary>,
}

/// A submitted backtest launch that continues as an asynchronous job.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LaunchAcceptedResponse {
    /// Job identifier.
    pub job_id: String,
    /// Current job state.
    pub state: harness_execution::jobs::JobState,
}

/// The persisted state of one launch job.
pub type JobStatusResponse = JobRecord;

/// Build the error envelope used by every failure response.
#[must_use]
pub fn error_body(code: &str, message: &str, details: Option<Value>) -> Value {
    let mut error = json!({ "code": code, "message": message });
    if let (Some(details), Some(object)) = (details, error.as_object_mut()) {
        object.insert("details".to_string(), details);
    }
    json!({ "error": error })
}

/// An HTTP error rendered with the harness error envelope.
#[derive(Debug, Clone)]
pub struct ApiError {
    /// HTTP status code.
    pub status: StatusCode,
    /// Stable machine code.
    pub code: String,
    /// Human-readable message.
    pub message: String,
    /// Optional structured details.
    pub details: Option<Value>,
}

impl ApiError {
    /// Construct an error.
    #[must_use]
    pub fn new(status: StatusCode, code: &str, message: &str) -> Self {
        Self {
            status,
            code: code.to_string(),
            message: message.to_string(),
            details: None,
        }
    }

    /// Attach structured details.
    #[must_use]
    pub fn with_details(mut self, details: Value) -> Self {
        self.details = Some(details);
        self
    }

    /// A 404 for a missing artifact.
    #[must_use]
    pub fn artifact_not_found() -> Self {
        Self::new(
            StatusCode::NOT_FOUND,
            "artifact_not_found",
            "the requested artifact is not part of the job",
        )
    }

    /// A 404 for a missing job.
    #[must_use]
    pub fn job_not_found() -> Self {
        Self::new(
            StatusCode::NOT_FOUND,
            "job_not_found",
            "the requested job does not exist",
        )
    }

    /// A 409 for a job ID submitted with different accepted input.
    #[must_use]
    pub fn job_conflict(detail: &str) -> Self {
        Self::new(
            StatusCode::CONFLICT,
            "job_conflict",
            "the job ID already exists with different input",
        )
        .with_details(json!(detail))
    }

    /// A 500 for a durable-store admission or publication failure.
    #[must_use]
    pub fn store_error(detail: &str) -> Self {
        Self::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "store_error",
            "the job could not be persisted",
        )
        .with_details(json!(detail))
    }

    /// A 422 for a request-validation failure.
    #[must_use]
    pub fn validation_error(message: &str) -> Self {
        Self::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            message,
        )
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(error_body(&self.code, &self.message, self.details)),
        )
            .into_response()
    }
}
