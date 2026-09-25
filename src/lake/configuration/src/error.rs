//! Configuration loading failures.

use core::fmt;

/// A configuration failure with the Python-style message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError {
    /// The failure message.
    pub message: String,
}

impl ConfigError {
    /// Build a configuration failure.
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ConfigError {}

impl From<std::io::Error> for ConfigError {
    fn from(error: std::io::Error) -> Self {
        Self::new(error.to_string())
    }
}

impl From<ConfigError> for lake_contracts::DataError {
    fn from(error: ConfigError) -> Self {
        lake_contracts::DataError::value(error.message)
    }
}
