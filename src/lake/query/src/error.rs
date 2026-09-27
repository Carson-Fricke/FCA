//! The stable application error translated by the HTTP boundary.
//!
//! Ports `lake.query.application.QueryApplicationError`: a machine-readable
//! `code`, the Python-identical human-readable `message`, and optional
//! structured `details`. The HTTP boundary maps the codes to status codes
//! (`coverage_incomplete` 409, `ensure_unavailable`/`sas_unavailable` 503,
//! `ensure_failed` 502, and anything else 422).

use std::fmt;

use lake_coverage::CoverageRequestError;
use serde_json::Value;

/// A file-query failure carrying the Python application error shape.
#[derive(Clone, Debug, PartialEq)]
pub struct QueryApplicationError {
    /// Stable machine-readable error code.
    pub code: String,
    /// Human-readable failure message.
    pub message: String,
    /// Optional structured context.
    pub details: Option<Value>,
}

impl QueryApplicationError {
    /// Build an application error with an optional structured context.
    pub fn new(
        code: impl Into<String>,
        message: impl Into<String>,
        details: Option<Value>,
    ) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            details,
        }
    }
}

impl fmt::Display for QueryApplicationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for QueryApplicationError {}

impl From<CoverageRequestError> for QueryApplicationError {
    /// Keep a coverage rejection's code, message, and details unchanged.
    fn from(error: CoverageRequestError) -> Self {
        Self::new(error.code, error.message, error.details)
    }
}

#[cfg(test)]
#[path = "../tests/unit/error.rs"]
mod tests;
