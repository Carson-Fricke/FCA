//! Authoritative harness document syntax, replay schema, and typed normalization.
//!
//! Compact shipped replay defaults are applied only to absent fields; invalid
//! supplied fields fail validation. Hash encoding lives in `harness-contracts`.

pub mod loader;
pub mod models;
pub mod replay;
pub mod yaml;

use std::fmt;

use harness_contracts::HashEncoding;
use serde_json::Value;

/// The domain separator for configuration hashes.
pub use harness_contracts::CONFIGURATION_HASH_DOMAIN;

/// A configuration loading or validation failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

/// Render a JSON value with sorted object keys and compact separators.
///
/// Reproduces `json.dumps(..., ensure_ascii=False, allow_nan=False,
/// separators=(",", ":"), sort_keys=True)`.
#[must_use]
pub fn canonical_json(value: &Value) -> String {
    harness_contracts::canonical_json(value, HashEncoding::Utf8)
}

/// Return a domain-separated SHA-256 hash of canonical configuration.
#[must_use]
pub fn configuration_hash(value: &Value) -> String {
    harness_contracts::configuration_hash(value, HashEncoding::Utf8)
}

/// One validated configuration document as its canonical tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HarnessConfig {
    /// An API shell document.
    Api(Value),
    /// A replay shell document (backtest or study).
    Backtest(Value),
    /// A trader shell document.
    Trader(Value),
}

impl HarnessConfig {
    /// The discriminated shell name.
    #[must_use]
    pub fn shell(&self) -> &'static str {
        match self {
            Self::Api(_) => "api",
            Self::Backtest(value) => {
                if value["shell"] == "study" {
                    "study"
                } else {
                    "backtest"
                }
            }
            Self::Trader(_) => "trader",
        }
    }

    /// The canonical `model_dump(mode="python")` tree.
    #[must_use]
    pub fn canonical_value(&self) -> &Value {
        match self {
            Self::Api(value) | Self::Backtest(value) | Self::Trader(value) => value,
        }
    }

    /// The canonical JSON document.
    #[must_use]
    pub fn canonical_json(&self) -> String {
        canonical_json(self.canonical_value())
    }

    /// The domain-separated configuration hash.
    #[must_use]
    pub fn configuration_hash(&self) -> String {
        configuration_hash(self.canonical_value())
    }
}

pub use loader::{
    environment_variable_name, load_configuration, load_example, resolve_environment_reference,
};
pub use yaml::{load_yaml_mapping, parse_yaml_mapping, YamlValue};
