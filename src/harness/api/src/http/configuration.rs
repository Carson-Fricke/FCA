//! Submitted backtest-configuration decoding for the launch route.
//!
//! The full typed configuration package lands with its own port task; until
//! then this module provides the route-facing seam. The default parser mirrors
//! the observable behavior of `parse_backtest_configuration_json`: the body
//! must be a JSON object, its `shell` must be `backtest`, unknown top-level
//! fields are rejected, and the fields the HTTP boundary needs (`run_id`,
//! `strategy_instances`, `start`, `end`) are validated. The configuration
//! digest uses the same domain-separated canonical JSON convention as
//! `harness.configuration.hashing`.

use std::fmt;

use chrono::{DateTime, NaiveDate, Utc};
use serde_json::Value;
use sha2::{Digest, Sha256};

const CONFIGURATION_HASH_DOMAIN: &[u8] = b"fca:harness-configuration:v1\0";

/// Top-level keys accepted by a `backtest` shell document.
const BACKTEST_KEYS: &[&str] = &[
    "schema_version",
    "shell",
    "run_id",
    "start",
    "end",
    "clock",
    "event_ordering",
    "seed",
    "base_currency",
    "initial_cash",
    "data",
    "strategy_instances",
    "risk_pipeline",
    "fees",
    "deployment",
];

/// A validated backtest configuration reduced to the HTTP boundary's needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidatedConfiguration {
    /// Run identifier.
    pub run_id: String,
    /// Strategy instance identifiers, sorted.
    pub strategy_ids: Vec<String>,
    /// First session date, when a `start` timestamp is present.
    pub data_start: Option<NaiveDate>,
    /// Last session date, when an `end` timestamp is present.
    pub data_end: Option<NaiveDate>,
    /// Domain-separated digest of the submitted document.
    pub configuration_digest: String,
    /// The submitted document, retained for the native engine launcher.
    pub document: Value,
}

/// A submitted document that is not a valid backtest configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigurationError(pub String);

impl fmt::Display for ConfigurationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ConfigurationError {}

/// Boundary that turns a submitted configuration body into a validated doc.
pub trait ConfigurationParser: Send + Sync {
    /// Decode and validate one submitted JSON configuration document.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigurationError`] when the document is malformed, uses the
    /// wrong shell, carries unknown fields, or misses required values.
    fn parse(&self, body: &[u8]) -> Result<ValidatedConfiguration, ConfigurationError>;
}

/// Strict JSON backtest-configuration parser used by the default app.
#[derive(Debug, Clone, Copy, Default)]
pub struct StrictJsonConfigurationParser;

/// Compute the domain-separated SHA-256 of a configuration document.
///
/// Note: this hashes the raw submitted JSON canonically. The typed
/// configuration port (T277) will replace this with the full validated-model
/// hash; the domain and algorithm already match.
#[must_use]
pub fn configuration_hash(document: &Value) -> String {
    let mut hasher = Sha256::new();
    hasher.update(CONFIGURATION_HASH_DOMAIN);
    hasher.update(super::store::canonical_json(document).as_bytes());
    hex::encode(hasher.finalize())
}

fn parse_rfc3339_date(value: &Value, field: &str) -> Result<Option<NaiveDate>, ConfigurationError> {
    match value {
        Value::Null => Ok(None),
        Value::String(text) => {
            let parsed = DateTime::parse_from_rfc3339(text).map_err(|error| {
                ConfigurationError(format!("{field} is not an RFC 3339 timestamp: {error}"))
            })?;
            Ok(Some(parsed.with_timezone(&Utc).date_naive()))
        }
        _ => Err(ConfigurationError(format!(
            "{field} must be an RFC 3339 string"
        ))),
    }
}

impl ConfigurationParser for StrictJsonConfigurationParser {
    fn parse(&self, body: &[u8]) -> Result<ValidatedConfiguration, ConfigurationError> {
        let document: Value = serde_json::from_slice(body)
            .map_err(|error| ConfigurationError(format!("malformed JSON: {error}")))?;
        let object = document.as_object().ok_or_else(|| {
            ConfigurationError("the submitted document is not a JSON object".to_string())
        })?;

        let shell = object
            .get("shell")
            .and_then(Value::as_str)
            .ok_or_else(|| ConfigurationError("configuration has no 'shell' key".to_string()))?;
        if shell != "backtest" {
            return Err(ConfigurationError(format!(
                "configuration shell must be 'backtest', not '{shell}'"
            )));
        }

        for key in object.keys() {
            if !BACKTEST_KEYS.contains(&key.as_str()) {
                return Err(ConfigurationError(format!(
                    "unexpected configuration field '{key}'"
                )));
            }
        }

        let run_id = object
            .get("run_id")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| ConfigurationError("run_id must be a non-empty string".to_string()))?
            .to_string();

        let mut strategy_ids = Vec::new();
        if let Some(instances) = object.get("strategy_instances") {
            let instances = instances.as_array().ok_or_else(|| {
                ConfigurationError("strategy_instances must be an array".to_string())
            })?;
            for instance in instances {
                let instance = instance.as_object().ok_or_else(|| {
                    ConfigurationError("each strategy instance must be an object".to_string())
                })?;
                let instance_id = instance
                    .get("instance_id")
                    .and_then(Value::as_str)
                    .filter(|value| !value.trim().is_empty())
                    .ok_or_else(|| {
                        ConfigurationError("strategy instance requires an instance_id".to_string())
                    })?;
                strategy_ids.push(instance_id.to_string());
            }
        }
        strategy_ids.sort();

        let data_start = match object.get("start") {
            Some(value) => parse_rfc3339_date(value, "start")?,
            None => None,
        };
        let data_end = match object.get("end") {
            Some(value) => parse_rfc3339_date(value, "end")?,
            None => None,
        };

        Ok(ValidatedConfiguration {
            run_id,
            strategy_ids,
            data_start,
            data_end,
            configuration_digest: configuration_hash(&document),
            document,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn parse(body: Value) -> Result<ValidatedConfiguration, ConfigurationError> {
        StrictJsonConfigurationParser.parse(body.to_string().as_bytes())
    }

    #[test]
    fn parses_a_backtest_document() {
        let configuration = parse(json!({
            "schema_version": 1,
            "shell": "backtest",
            "run_id": "run-1",
            "start": "2024-01-02T14:30:00Z",
            "end": "2024-01-19T21:00:00Z",
            "strategy_instances": [
                {"instance_id": "z"},
                {"instance_id": "a"}
            ]
        }))
        .expect("parse");
        assert_eq!(configuration.run_id, "run-1");
        assert_eq!(configuration.strategy_ids, vec!["a", "z"]);
        assert_eq!(
            configuration.data_start,
            NaiveDate::from_ymd_opt(2024, 1, 2)
        );
        assert_eq!(configuration.data_end, NaiveDate::from_ymd_opt(2024, 1, 19));
        assert_eq!(configuration.configuration_digest.len(), 64);
    }

    #[test]
    fn rejects_wrong_shell_and_unknown_fields() {
        assert!(parse(json!({"shell": "trader"})).is_err());
        assert!(parse(json!({"shell": "backtest", "run_id": "r", "unexpected": true})).is_err());
        assert!(parse(json!([1, 2, 3])).is_err());
    }

    #[test]
    fn hash_is_stable() {
        let document = json!({"shell": "backtest", "run_id": "r"});
        assert_eq!(configuration_hash(&document), configuration_hash(&document));
    }
}
