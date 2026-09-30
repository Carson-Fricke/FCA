//! Provider error taxonomy.
//!
//! Collapses the Python `AlpacaHTTPError`/`AlpacaProtocolError` split into one
//! enum so callers can match a coarse category while carrying the exact
//! Python-style message. Transport and timeout failures are transient; HTTP,
//! protocol, and configuration failures are terminal.

use thiserror::Error;

/// A provider failure.
#[derive(Clone, Debug, PartialEq, Eq, Error)]
pub enum ProviderError {
    /// A non-successful Alpaca response after the retry policy was exhausted.
    #[error("Alpaca request failed with HTTP {status}: {url}")]
    Http {
        /// The response status code.
        status: u16,
        /// The request URL.
        url: String,
    },
    /// The transport could not complete the request.
    #[error("{0}")]
    Transport(String),
    /// The request exceeded the configured timeout.
    #[error("{0}")]
    Timeout(String),
    /// The provider returned a malformed response or a looping cursor.
    #[error("{0}")]
    Protocol(String),
    /// The configuration or request arguments are invalid.
    #[error("{0}")]
    Configuration(String),
}

impl ProviderError {
    /// Whether retrying the same request can succeed.
    pub fn is_transient(&self) -> bool {
        matches!(self, Self::Transport(_) | Self::Timeout(_))
    }
}
