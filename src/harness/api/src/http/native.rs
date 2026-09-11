//! Native Rust backtest runner for the harness HTTP launch route.
//!
//! [`NativeBacktestRunner`] plugs the ported Rust engine into the
//! [`BacktestRunner`] seam used by [`crate::http::launcher::InProcessLauncher`].
//! The submitted JSON configuration is decoded into the engine's typed
//! [`BacktestConfig`] through the same YAML value model the file loader uses,
//! then the engine core drives the configured Python strategies through the
//! `harness-host` PyO3 adapter — the same entry point the `backtest-job`
//! command uses.

use std::path::PathBuf;

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::yaml::YamlValue;
use num::PyDecimal;
use serde_json::Value;

use super::configuration::ValidatedConfiguration;
use super::launcher::{BacktestRunner, RunOutcome};

/// Native Rust backtest runner used by the durable API composition.
#[derive(Debug, Clone, Default)]
pub struct NativeBacktestRunner {
    /// Optional directory the engine writes its own artifact set beneath.
    pub output_root: Option<PathBuf>,
}

impl NativeBacktestRunner {
    /// Compose a runner writing engine artifacts beneath `output_root`.
    #[must_use]
    pub fn with_output_root(output_root: PathBuf) -> Self {
        Self {
            output_root: Some(output_root),
        }
    }
}

impl BacktestRunner for NativeBacktestRunner {
    fn run(&self, configuration: &ValidatedConfiguration) -> Result<RunOutcome, String> {
        let yaml = json_to_yaml(&configuration.document);
        let native = BacktestConfig::from_yaml_value(&yaml).map_err(|error| error.to_string())?;
        let directory = match &self.output_root {
            Some(root) => root.join(&native.run_id),
            None => std::env::temp_dir()
                .join("harness-api-native")
                .join(&native.run_id),
        };
        let (outcome, _artifact_path) =
            host::backtest::run_backtest_config(&native, Some(directory.as_path()), None)
                .map_err(|error| error.to_string())?;
        Ok(RunOutcome {
            completed: outcome.completed(),
            artifact_json: outcome.artifact_json().into_bytes(),
        })
    }
}

fn decimal_value(text: &str) -> YamlValue {
    match text.parse::<PyDecimal>() {
        Ok(value) => YamlValue::Decimal(value),
        Err(_) => YamlValue::String(text.to_string()),
    }
}

/// Convert one submitted JSON value into the engine's YAML value model.
///
/// Objects and arrays recurse; JSON integers stay integers, JSON floats become
/// exact decimals, and strings stay strings (so strategy parameters such as
/// `"0.05"` remain quoted strings rather than tagged decimals).
#[must_use]
pub fn json_to_yaml(value: &Value) -> YamlValue {
    match value {
        Value::Null => YamlValue::Null,
        Value::Bool(inner) => YamlValue::Bool(*inner),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                YamlValue::Int(integer)
            } else if let Some(unsigned) = number.as_u64() {
                i64::try_from(unsigned)
                    .map_or_else(|_| decimal_value(&unsigned.to_string()), YamlValue::Int)
            } else {
                decimal_value(&number.to_string())
            }
        }
        Value::String(text) => YamlValue::String(text.clone()),
        Value::Array(items) => YamlValue::Sequence(items.iter().map(json_to_yaml).collect()),
        Value::Object(entries) => YamlValue::Mapping(
            entries
                .iter()
                .map(|(key, item)| (key.clone(), json_to_yaml(item)))
                .collect(),
        ),
    }
}

#[cfg(test)]
#[path = "../../tests/unit/http_native.rs"]
mod tests;
