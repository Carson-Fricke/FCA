//! Canonical configuration identity for one strategy-instance parameter point.
//!
//! [`configuration_json`] renders a backtest configuration with one strategy
//! instance's parameters merged over its declared values, and
//! [`configuration_digest`] hashes that document with the shared UTF-8 v1
//! configuration hasher. The digest is the identity of a parameter point; it
//! changes only when the rendered configuration changes.

use std::collections::BTreeMap;

use crate::backtest::config::BacktestConfig;
use harness_contracts::canonical_json::JsonValue;

fn merge_parameters(
    base: &BacktestConfig,
    instance_id: &str,
    parameters: Option<&BTreeMap<String, JsonValue>>,
) -> BTreeMap<String, JsonValue> {
    let mut merged = base
        .strategy_instances
        .iter()
        .find(|instance| instance.instance_id == instance_id)
        .map(|instance| instance.parameters.clone())
        .unwrap_or_default();
    if let Some(parameters) = parameters {
        for (key, value) in parameters {
            merged.insert(key.clone(), value.clone());
        }
    }
    merged
}

/// Canonical JSON for a configuration with one instance's parameters merged.
#[must_use]
pub fn configuration_json(
    base: &BacktestConfig,
    instance_id: &str,
    parameters: Option<&BTreeMap<String, JsonValue>>,
) -> JsonValue {
    let mut instances = Vec::new();
    for instance in &base.strategy_instances {
        let merged = merge_parameters(base, &instance.instance_id, None);
        let parameters = if instance.instance_id == instance_id {
            JsonValue::Object(merge_parameters(base, instance_id, parameters))
        } else {
            JsonValue::Object(merged)
        };
        instances.push(JsonValue::object([
            (
                "instance_id",
                JsonValue::String(instance.instance_id.clone()),
            ),
            (
                "implementation",
                JsonValue::String(instance.implementation.clone()),
            ),
            (
                "bundle_hash",
                JsonValue::String(instance.bundle_hash.clone()),
            ),
            ("parameters", parameters),
            (
                "symbols",
                JsonValue::Array(
                    instance
                        .symbols
                        .iter()
                        .map(|symbol| JsonValue::String(symbol.clone()))
                        .collect(),
                ),
            ),
            (
                "capital_fraction",
                JsonValue::String(instance.capital_fraction.clone()),
            ),
        ]));
    }
    JsonValue::object([
        ("job_id", JsonValue::String(base.job_id.clone())),
        ("shell", JsonValue::String(base.shell.clone())),
        ("schema_version", JsonValue::UInt(base.schema_version)),
        ("seed", JsonValue::UInt(base.seed)),
        ("start_ns", JsonValue::Int(base.start_ns)),
        ("end_ns", JsonValue::Int(base.end_ns)),
        (
            "base_currency",
            JsonValue::String(base.base_currency.clone()),
        ),
        ("initial_cash", JsonValue::String(base.initial_cash.clone())),
        (
            "symbols",
            JsonValue::Array(
                base.symbols
                    .iter()
                    .map(|symbol| JsonValue::String(symbol.clone()))
                    .collect(),
            ),
        ),
        ("strategy_instances", JsonValue::Array(instances)),
    ])
}

/// Return the canonical configuration hash of one parameter point.
#[must_use]
pub fn configuration_digest(
    base: &BacktestConfig,
    instance_id: &str,
    parameters: &BTreeMap<String, JsonValue>,
) -> String {
    harness_contracts::configuration_hash(
        &configuration_json(base, instance_id, Some(parameters)).to_serde_value(),
        harness_contracts::HashEncoding::Utf8,
    )
}
