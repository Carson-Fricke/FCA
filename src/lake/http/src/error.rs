//! The error envelope shared by every market-lake HTTP response.
//!
//! Mirrors the Python FastAPI handlers: every failure renders
//! `{"error": {"code", "message"}}`, a request-validation failure adds a
//! FastAPI-style `details` array, and a 401 adds `WWW-Authenticate: Bearer`.
//! `HTTPException` details are not forwarded, exactly as the Python exception
//! handler drops them.

use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use lake_contracts::DataError;
use lake_coverage::CoverageRequestError;
use lake_query::QueryApplicationError;
use serde_json::{json, Value};

/// Build the shared error envelope.
#[must_use]
pub fn error_body(code: &str, message: &str, details: Option<Value>) -> Value {
    let mut error = json!({ "code": code, "message": message });
    if let (Some(details), Some(object)) = (details, error.as_object_mut()) {
        object.insert("details".to_string(), details);
    }
    json!({ "error": error })
}

/// An HTTP failure rendered with the market-lake error envelope.
#[derive(Debug, Clone)]
pub struct ApiError {
    /// HTTP status code.
    pub status: StatusCode,
    /// Stable machine-readable code.
    pub code: String,
    /// Human-readable message.
    pub message: String,
    /// FastAPI-style validation details.
    pub details: Option<Value>,
    /// Whether the response carries `WWW-Authenticate: Bearer`.
    unauthorized: bool,
}

impl ApiError {
    /// Construct an error without structured details.
    #[must_use]
    pub fn new(status: StatusCode, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.into(),
            message: message.into(),
            details: None,
            unauthorized: false,
        }
    }

    /// Attach a FastAPI-style details array.
    #[must_use]
    pub fn with_details(mut self, details: Value) -> Self {
        self.details = Some(details);
        self
    }

    /// A 422 whose message is Python's uniform validation failure.
    #[must_use]
    pub fn validation_error(location: &str, message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            "request validation failed",
        )
        .with_details(json!([{
            "loc": [location],
            "msg": message.into(),
            "type": "value_error",
        }]))
    }

    /// The uniform 401 rendered for a missing or invalid credential.
    #[must_use]
    pub fn unauthorized() -> Self {
        Self {
            status: StatusCode::UNAUTHORIZED,
            code: "unauthorized".to_string(),
            message: "a valid bearer token is required".to_string(),
            details: None,
            unauthorized: true,
        }
    }

    /// A 404 for an unknown ensure job.
    #[must_use]
    pub fn job_not_found() -> Self {
        Self::new(
            StatusCode::NOT_FOUND,
            "job_not_found",
            "the requested job does not exist",
        )
    }

    /// A 404 for an unmatched route, rendered exactly as FastAPI does.
    #[must_use]
    pub fn not_found() -> Self {
        Self::new(StatusCode::NOT_FOUND, "http_error", "Not Found")
    }

    /// A 405 for a matched path with an unsupported method.
    #[must_use]
    pub fn method_not_allowed() -> Self {
        Self::new(
            StatusCode::METHOD_NOT_ALLOWED,
            "http_error",
            "Method Not Allowed",
        )
    }

    /// A 500 for a repository or internal failure.
    #[must_use]
    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, "internal_error", message)
    }

    /// A 503 when file-query SAS issuance is not configured.
    #[must_use]
    pub fn sas_unavailable(message: impl Into<String>) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, "sas_unavailable", message)
    }

    /// Map a coverage policy rejection onto the Python 422 envelope.
    ///
    /// The Python exception handler forwards only the code and message, so
    /// structured details are intentionally dropped.
    #[must_use]
    pub fn from_coverage(error: &CoverageRequestError) -> Self {
        Self::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            error.code.clone(),
            error.message.clone(),
        )
    }

    /// Map a query-application rejection onto the Python status table.
    #[must_use]
    pub fn from_query(error: &QueryApplicationError) -> Self {
        let status = match error.code.as_str() {
            "coverage_incomplete" => StatusCode::CONFLICT,
            "ensure_failed" => StatusCode::BAD_GATEWAY,
            "ensure_unavailable" | "query_unavailable" | "sas_unavailable" => {
                StatusCode::SERVICE_UNAVAILABLE
            }
            _ => StatusCode::UNPROCESSABLE_ENTITY,
        };
        Self::new(status, error.code.clone(), error.message.clone())
    }
}

impl From<DataError> for ApiError {
    fn from(error: DataError) -> Self {
        Self::internal(error.to_string())
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut response = (
            self.status,
            Json(error_body(&self.code, &self.message, self.details)),
        )
            .into_response();
        if self.unauthorized {
            response
                .headers_mut()
                .insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Bearer"));
        }
        response
    }
}
