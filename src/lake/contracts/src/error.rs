//! Shared data-layer failure type.
//!
//! The Python lake raises small, purpose-specific exceptions. The Rust port
//! keeps one enum so callers can match a coarse category while carrying the
//! exact Python-style message for parity assertions.

use core::fmt;

/// A data-layer failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DataError {
    /// A caller passed an argument of the wrong Python-level type.
    TypeError(String),
    /// A caller passed a value that fails validation.
    ValueError(String),
    /// A point-in-time read asked for data newer than the engine clock head.
    Lookahead(String),
    /// The market lake did not return every requested partition.
    Incomplete(String),
    /// The request is outside the supported market-lake contract.
    Unsupported(String),
    /// A terminal ensure job failed.
    EnsureJob(String),
    /// Fixed-point arithmetic overflowed, divided by zero, or failed to parse.
    Arithmetic(String),
    /// A provider stream or HTTP peer sent an invalid frame.
    Protocol(String),
    /// A required object does not exist.
    NotFound(String),
    /// An object already exists or state conflicts with the request.
    Conflict(String),
    /// A filesystem or object-store operation failed.
    Io(String),
}

impl DataError {
    /// Build a [`DataError::ValueError`].
    pub fn value(message: impl Into<String>) -> Self {
        Self::ValueError(message.into())
    }

    /// Build a [`DataError::TypeError`].
    pub fn type_error(message: impl Into<String>) -> Self {
        Self::TypeError(message.into())
    }

    /// Build a [`DataError::NotFound`].
    pub fn not_found(message: impl Into<String>) -> Self {
        Self::NotFound(message.into())
    }

    /// Build a [`DataError::Conflict`].
    pub fn conflict(message: impl Into<String>) -> Self {
        Self::Conflict(message.into())
    }

    /// Build a [`DataError::Io`].
    pub fn io(error: impl fmt::Display) -> Self {
        Self::Io(error.to_string())
    }

    /// Build a [`DataError::Protocol`].
    pub fn protocol(message: impl Into<String>) -> Self {
        Self::Protocol(message.into())
    }
}

impl fmt::Display for DataError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TypeError(message)
            | Self::ValueError(message)
            | Self::Lookahead(message)
            | Self::Incomplete(message)
            | Self::Unsupported(message)
            | Self::EnsureJob(message)
            | Self::Arithmetic(message)
            | Self::Protocol(message)
            | Self::NotFound(message)
            | Self::Conflict(message)
            | Self::Io(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for DataError {}

impl From<std::io::Error> for DataError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error.to_string())
    }
}
