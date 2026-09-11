//! Error types shared by the data layer.
//!
//! The Python data layer raises small, purpose-specific exceptions. The Rust
//! port keeps one enum so callers can match on a coarse category while still
//! carrying the exact Python-style message for parity assertions.

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
    /// Fixed-point arithmetic overflowed or divided by zero.
    Arithmetic(String),
    /// A provider stream sent an invalid frame or rejected an operation.
    Protocol(String),
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
            | Self::Protocol(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for DataError {}

impl From<harness_num::DecimalError> for DataError {
    fn from(error: harness_num::DecimalError) -> Self {
        Self::Arithmetic(error.to_string())
    }
}
