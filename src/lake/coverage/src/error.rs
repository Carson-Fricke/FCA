//! The coverage application rejection type.
//!
//! Ports `lake.coverage.application.CoverageRequestError`: a stable
//! machine-readable `code`, the Python-identical human-readable `message`, and
//! optional structured `details`.

use std::fmt;

use lake_contracts::DataError;
use serde_json::Value;

/// A coverage request rejected by an application policy.
#[derive(Clone, Debug, PartialEq)]
pub struct CoverageRequestError {
    /// Stable machine-readable policy code.
    pub code: String,
    /// Human-readable rejection message.
    pub message: String,
    /// Optional structured context.
    pub details: Option<Value>,
}

impl CoverageRequestError {
    /// Build a rejection with an optional structured context.
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

impl fmt::Display for CoverageRequestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for CoverageRequestError {}

impl From<DataError> for CoverageRequestError {
    fn from(error: DataError) -> Self {
        Self::new("internal_error", error.to_string(), None)
    }
}
