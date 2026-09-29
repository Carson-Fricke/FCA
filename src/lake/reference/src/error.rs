//! Purpose-specific reference data failures.
//!
//! The Python lake raises `EdgarParseError`, `NasdaqParseError`,
//! `ReferenceHTTPError`, and plain `ValueError`s from the reference package.
//! The Rust port keeps one enum so callers can match a coarse category while
//! carrying the Python-style message for parity assertions.

use core::fmt;

use lake_configuration::ConfigError;
use lake_contracts::DataError;

/// A reference ingest or read failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReferenceError {
    /// The payload is not a parsable SEC XBRL facts document.
    Edgar(String),
    /// The symbol directory payload is not a parsable nasdaqtraded.txt file.
    Nasdaq(String),
    /// The payload is not a parsable SEC company tickers document.
    Tickers(String),
    /// A non-successful provider response after the retry policy is exhausted,
    /// or an undecodable payload.
    Http {
        /// The HTTP status code that ended the retry policy.
        status: u16,
        /// The requested URL.
        url: String,
    },
    /// A transport-level failure that never produced a usable response.
    Transport(String),
    /// Reference configuration loading or validation failed.
    Configuration(String),
    /// A reference object carries an unexpected schema.
    Schema(String),
    /// A caller passed a value that fails validation.
    Invalid(String),
    /// A storage, catalog, or data-layer failure.
    Data(DataError),
}

impl ReferenceError {
    /// Build a [`ReferenceError::Edgar`].
    pub fn edgar(message: impl Into<String>) -> Self {
        Self::Edgar(message.into())
    }

    /// Build a [`ReferenceError::Nasdaq`].
    pub fn nasdaq(message: impl Into<String>) -> Self {
        Self::Nasdaq(message.into())
    }

    /// Build a [`ReferenceError::Tickers`].
    pub fn tickers(message: impl Into<String>) -> Self {
        Self::Tickers(message.into())
    }

    /// Build a [`ReferenceError::Invalid`].
    pub fn invalid(message: impl Into<String>) -> Self {
        Self::Invalid(message.into())
    }
}

impl fmt::Display for ReferenceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Edgar(message)
            | Self::Nasdaq(message)
            | Self::Tickers(message)
            | Self::Transport(message)
            | Self::Configuration(message)
            | Self::Invalid(message) => formatter.write_str(message),
            Self::Http { status, url } => {
                write!(
                    formatter,
                    "reference request failed with HTTP {status}: {url}"
                )
            }
            Self::Schema(message) => formatter.write_str(message),
            Self::Data(error) => formatter.write_str(&error.to_string()),
        }
    }
}

impl std::error::Error for ReferenceError {}

impl From<DataError> for ReferenceError {
    fn from(error: DataError) -> Self {
        Self::Data(error)
    }
}

impl From<std::io::Error> for ReferenceError {
    fn from(error: std::io::Error) -> Self {
        Self::Data(DataError::io(error))
    }
}

impl From<ConfigError> for ReferenceError {
    fn from(error: ConfigError) -> Self {
        Self::Configuration(error.message)
    }
}
