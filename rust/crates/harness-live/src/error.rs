//! Deterministic error surface for the live-trading port.
//!
//! The Python engine raises a family of dedicated exception types. The Rust
//! port keeps the same distinctions as [`LiveError`] variants so callers can
//! match on the failure without parsing strings, while a single `Display`
//! implementation keeps messages stable.

use core::fmt;

/// Result alias used across the `harness-live` crate.
pub type LiveResult<T> = Result<T, LiveError>;

/// A deterministic live-trading validation failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LiveError {
    /// Generic domain validation failure (non-empty text, positivity, ...).
    Validation(String),
    /// An execution report cannot follow the current lifecycle state.
    IllegalTransition(String),
    /// Cumulative or last-fill report values are inconsistent.
    ExecutionLedger(String),
    /// A report identifier was applied more than once.
    DuplicateExecutionReport(String),
    /// An order identifier was already submitted or reserved.
    DuplicateOrder(String),
    /// A replacement request or report is inconsistent.
    Replace(String),
    /// An order identifier is not tracked.
    UnknownOrder(String),
    /// A multi-leg group lifecycle operation is invalid.
    GroupTransition(String),
    /// A candidate batch is not a complete valid group fill.
    GroupFill(String),
    /// A group identifier was already submitted.
    DuplicateGroup(String),
    /// A group identifier is not tracked.
    UnknownGroup(String),
    /// A fill-model failure.
    Fill(String),
    /// A fee-model failure.
    Fee(String),
    /// A venue-adapter failure.
    Venue(String),
    /// A portfolio-accounting failure.
    Portfolio(String),
    /// A risk-engine failure.
    Risk(String),
}

impl fmt::Display for LiveError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Validation(message) => write!(formatter, "{message}"),
            Self::IllegalTransition(message) => write!(formatter, "{message}"),
            Self::ExecutionLedger(message) => write!(formatter, "{message}"),
            Self::DuplicateExecutionReport(message) => write!(formatter, "{message}"),
            Self::DuplicateOrder(message) => write!(formatter, "{message}"),
            Self::Replace(message) => write!(formatter, "{message}"),
            Self::UnknownOrder(message) => write!(formatter, "unknown order {message}"),
            Self::GroupTransition(message) => write!(formatter, "{message}"),
            Self::GroupFill(message) => write!(formatter, "{message}"),
            Self::DuplicateGroup(message) => write!(formatter, "{message}"),
            Self::UnknownGroup(message) => write!(formatter, "unknown group {message}"),
            Self::Fill(message) => write!(formatter, "{message}"),
            Self::Fee(message) => write!(formatter, "{message}"),
            Self::Venue(message) => write!(formatter, "{message}"),
            Self::Portfolio(message) => write!(formatter, "{message}"),
            Self::Risk(message) => write!(formatter, "{message}"),
        }
    }
}

impl std::error::Error for LiveError {}

/// Return a trimmed, non-empty copy of `value`.
pub fn require_text(value: &str, field: &str) -> LiveResult<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(LiveError::Validation(format!(
            "{field} must be a non-empty string"
        )));
    }
    Ok(trimmed.to_string())
}
