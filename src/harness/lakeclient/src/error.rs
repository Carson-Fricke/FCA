//! Typed market-lake client failures.

use std::fmt;

use thiserror::Error;

/// One symbol/data-type window the lake could not complete.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnsureFailure {
    /// The requested symbol.
    pub symbol: String,
    /// The requested asset class.
    pub asset_class: String,
    /// The requested data type.
    pub data_type: String,
    /// Inclusive first session date.
    pub start_date: String,
    /// Inclusive last session date.
    pub end_date: String,
    /// The underlying poll failure.
    pub detail: String,
}

impl fmt::Display for EnsureFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "market-lake could not fetch {} {}/{} for {}..{}: {}",
            self.symbol,
            self.asset_class,
            self.data_type,
            self.start_date,
            self.end_date,
            self.detail
        )
    }
}

/// A failure from one market-lake client operation.
///
/// The display strings preserve the historical `LakeEnsureClient` messages for
/// the ensure and coverage paths, so call sites that surface them keep their
/// existing wording.
#[derive(Clone, Debug, Error, PartialEq, Eq)]
pub enum LakeClientError {
    /// The client is not configured (missing token or an unusable base URL).
    #[error("{0}")]
    Config(String),
    /// The HTTP transport failed before a response arrived.
    #[error("{0}")]
    Transport(String),
    /// The lake answered a non-success HTTP status.
    #[error("HTTP {status}: {body}")]
    Http {
        /// The HTTP status code.
        status: u16,
        /// The `error.code` value when the body carries the lake error envelope.
        code: Option<String>,
        /// The raw response body.
        body: String,
    },
    /// A response body did not match the published contract.
    #[error("{0}")]
    Protocol(String),
    /// The ensure request could not be issued.
    #[error("market-lake ensure request failed: {0}")]
    EnsureRequest(String),
    /// A coverage query failed.
    #[error("market-lake coverage query failed: {0}")]
    CoverageQuery(String),
    /// The ensure poll settled with coverage still incomplete.
    #[error("{0}")]
    EnsureIncomplete(String),
    /// One symbol/data-type window could not be completed.
    #[error("{0}")]
    EnsureFailed(Box<EnsureFailure>),
    /// The Arrow IPC response could not be decoded.
    #[error("market-lake Arrow IPC could not be decoded: {0}")]
    Decode(String),
}
