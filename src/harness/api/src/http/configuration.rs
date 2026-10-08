//! Submitted document validation delegates to the engine's authoritative schemas.

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::{normalize_subscriptions, MarketSubscription};
use backtest::backtest::screen::ScreenConfig;
use chrono::{DateTime, NaiveDate, Utc};
use serde_json::Value;
use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub struct ValidatedConfiguration {
    pub job_id: String,
    pub strategy_ids: Vec<String>,
    pub data_start: Option<NaiveDate>,
    pub data_end: Option<NaiveDate>,
    /// Submitted-document identity, not the engine's resolved configuration identity.
    pub configuration_digest: String,
    pub document: Value,
    pub screen: Option<ScreenConfig>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigurationError(pub String);

impl fmt::Display for ConfigurationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}
impl std::error::Error for ConfigurationError {}

pub trait ConfigurationParser: Send + Sync {
    fn parse(&self, body: &[u8]) -> Result<ValidatedConfiguration, ConfigurationError>;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct StrictJsonConfigurationParser;

/// Historical submitted-document UTF-8 v1 identity. Defaults are intentionally not hashed.
pub fn configuration_hash(document: &Value) -> String {
    config::configuration_hash(document)
}

pub(crate) fn parse_subscriptions(
    value: &Value,
) -> Result<Vec<MarketSubscription>, ConfigurationError> {
    // Tagged contract decoding rejects unknown types and interval on non-bars, including null.
    config::replay::validate_document(&serde_json::json!({
        "shell":"backtest", "job_id":"subscriptions",
        "data":{"subscriptions":value, "symbols":["SPY"]}
    }))
    .map_err(|e| ConfigurationError(e.0))?;
    let inputs = serde_json::from_value::<Vec<MarketSubscription>>(value.clone())
        .map_err(|e| ConfigurationError(e.to_string()))?;
    normalize_subscriptions(&inputs).map_err(|e| ConfigurationError(e.to_string()))
}

impl ConfigurationParser for StrictJsonConfigurationParser {
    fn parse(&self, body: &[u8]) -> Result<ValidatedConfiguration, ConfigurationError> {
        let document: Value = serde_json::from_slice(body)
            .map_err(|e| ConfigurationError(format!("malformed JSON: {e}")))?;
        let shell = document
            .get("shell")
            .and_then(Value::as_str)
            .ok_or_else(|| ConfigurationError("configuration has no 'shell' key".into()))?;
        let (job_id, mut strategy_ids, data_start, data_end, screen) = if shell == "screen" {
            let screen = ScreenConfig::from_json_value(&document)
                .map_err(|e| ConfigurationError(e.to_string()))?;
            (
                screen.job_id.clone(),
                vec![screen.definition_id.clone()],
                NaiveDate::parse_from_str(&screen.window_start(), "%Y-%m-%d").ok(),
                NaiveDate::parse_from_str(&screen.window_end(), "%Y-%m-%d").ok(),
                Some(screen),
            )
        } else {
            let configuration = BacktestConfig::from_yaml(&document.to_string())
                .map_err(|e| ConfigurationError(e.to_string()))?;
            let date = |key: &str| {
                document
                    .get(key)
                    .and_then(Value::as_str)
                    .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
                    .map(|v| v.with_timezone(&Utc).date_naive())
            };
            (
                configuration.job_id,
                configuration
                    .strategy_instances
                    .into_iter()
                    .map(|s| s.instance_id)
                    .collect(),
                date("start"),
                date("end"),
                None,
            )
        };
        strategy_ids.sort();
        Ok(ValidatedConfiguration {
            job_id,
            strategy_ids,
            data_start,
            data_end,
            configuration_digest: configuration_hash(&document),
            document,
            screen,
        })
    }
}
