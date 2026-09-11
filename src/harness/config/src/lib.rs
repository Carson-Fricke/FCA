//! Behavior-identical harness configuration: YAML-subset loading, strict typed
//! models, and canonical domain-separated hashing.
//!
//! This crate ports `harness.configuration` (hashing, models, loader) so the
//! Rust engine loads and hashes committed documents exactly as Python does.
//! `load_configuration` returns the canonical `model_dump(mode="python")` tree,
//! which both the hash and downstream consumers use.

pub mod loader;
pub mod models;
pub mod yaml;

use std::fmt;

use serde_json::Value;
use sha2::{Digest, Sha256};

/// The domain separator for configuration hashes.
pub const CONFIGURATION_HASH_DOMAIN: &[u8] = b"fca:harness-configuration:v1\0";

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
    let mut output = String::new();
    write_canonical(value, &mut output);
    output
}

fn write_canonical(value: &Value, output: &mut String) {
    match value {
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|left, right| left.0.cmp(right.0));
            output.push('{');
            for (index, (key, item)) in entries.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                output.push_str(&serde_json::to_string(key).unwrap_or_else(|_| "\"\"".to_string()));
                output.push(':');
                write_canonical(item, output);
            }
            output.push('}');
        }
        Value::Array(items) => {
            output.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                write_canonical(item, output);
            }
            output.push(']');
        }
        other => output.push_str(&other.to_string()),
    }
}

/// Return a domain-separated SHA-256 hash of canonical configuration.
#[must_use]
pub fn configuration_hash(value: &Value) -> String {
    let mut hasher = Sha256::new();
    hasher.update(CONFIGURATION_HASH_DOMAIN);
    hasher.update(canonical_json(value).as_bytes());
    hex::encode(hasher.finalize())
}

/// One validated configuration document as its canonical tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HarnessConfig {
    /// An API shell document.
    Api(Value),
    /// A backtest shell document.
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
            Self::Backtest(_) => "backtest",
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn canonical_json_sorts_keys() {
        let value = json!({"b": 1, "a": {"d": 2, "c": 3}});
        assert_eq!(canonical_json(&value), r#"{"a":{"c":3,"d":2},"b":1}"#);
    }

    #[test]
    fn hash_is_domain_separated() {
        let value = json!({"shell": "backtest"});
        let digest = configuration_hash(&value);
        assert_eq!(digest.len(), 64);
        assert_eq!(digest, configuration_hash(&value));
    }
}
