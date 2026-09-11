//! Errors surfaced by the embedded Python host.

use std::fmt;

/// A failure while loading or invoking a Python host artifact.
#[derive(Debug)]
pub enum HostError {
    /// A Python exception was raised.
    Python(String),
    /// A required attribute or callable was missing.
    MissingAttribute(String),
    /// Reading source or configuration failed.
    Io(String),
    /// A JSON payload could not be encoded or decoded.
    Json(String),
    /// The supplied input was not valid.
    Invalid(String),
}

impl fmt::Display for HostError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Python(message) => write!(formatter, "python error: {message}"),
            Self::MissingAttribute(message) => {
                write!(formatter, "missing python attribute: {message}")
            }
            Self::Io(message) => write!(formatter, "io error: {message}"),
            Self::Json(message) => write!(formatter, "json error: {message}"),
            Self::Invalid(message) => write!(formatter, "invalid host input: {message}"),
        }
    }
}

impl std::error::Error for HostError {}

impl From<pyo3::PyErr> for HostError {
    fn from(error: pyo3::PyErr) -> Self {
        Self::Python(error.to_string())
    }
}

impl From<std::io::Error> for HostError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error.to_string())
    }
}

impl From<serde_json::Error> for HostError {
    fn from(error: serde_json::Error) -> Self {
        Self::Json(error.to_string())
    }
}
