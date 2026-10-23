//! Builds replay provenance and configured program identities; no execution.
use crate::backtest::config::BacktestConfig;
use crate::backtest::fixture::DataPreflight;
use crate::backtest::fixture::ReplayFixture;
use crate::backtest::manifest::BacktestManifest;
use crate::backtest::manifest::StrategyIdentity;
use harness_contracts::canonical_json::JsonValue;
use harness_trading::replay_errors::BacktestError;

/// Build the reproducibility manifest for one native engine run.
///
/// # Errors
///
/// Returns [`BacktestError`] when the risk configuration is invalid.
pub fn build_default_manifest(
    configuration: &BacktestConfig,
    fixture: &ReplayFixture,
    preflight: &DataPreflight,
) -> Result<BacktestManifest, BacktestError> {
    let config_json = configuration.canonical_json();
    let JsonValue::Object(mut entries) = config_json else {
        unreachable!("configuration canonical json is an object");
    };
    let fees = entries.remove("fees").unwrap_or(JsonValue::Null);
    let risk = entries.remove("risk_pipeline").unwrap_or(JsonValue::Null);
    let rules = resolve_risk_rules(configuration)?;
    let rule_ids: Vec<JsonValue> = rules
        .iter()
        .map(|rule| JsonValue::String(rule.rule_id.clone()))
        .collect();
    let fill_identity = JsonValue::object([("model", JsonValue::string("simple_touch"))]);
    let fee_identity = JsonValue::object([
        ("fees", fees),
        ("precision", JsonValue::string("0.0001")),
        (
            "schedule_id",
            JsonValue::String("harness-config".to_string()),
        ),
    ]);
    let risk_identity =
        JsonValue::object([("pipeline", risk), ("rules", JsonValue::Array(rule_ids))]);
    crate::backtest::manifest::build_manifest(
        configuration,
        fixture,
        preflight,
        "simple_touch",
        &fill_identity,
        "harness-config",
        &fee_identity,
        &risk_identity,
        strategy_identities(configuration),
    )
}

/// Build the manifest strategy identities from configuration.
#[must_use]
pub fn strategy_identities(configuration: &BacktestConfig) -> Vec<StrategyIdentity> {
    configuration
        .strategy_instances
        .iter()
        .map(|instance| StrategyIdentity {
            instance_id: instance.instance_id.clone(),
            implementation: instance.implementation.clone(),
            bundle_hash: instance.bundle_hash.clone(),
            parameter_hash: crate::backtest::manifest::configuration_hash(&JsonValue::Object(
                instance.parameters.clone(),
            )),
            symbols: instance.symbols.clone(),
            capital_fraction: instance.capital_fraction.clone(),
        })
        .collect()
}

use super::risk::resolve_risk_rules;
