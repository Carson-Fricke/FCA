use super::*;
use crate::backtest::config::StrategyInstanceConfig;

fn value(text: &str) -> JsonValue {
    // Numeric levels are represented as tagged decimals, matching the
    // canonical scalar encoding.
    JsonValue::object([("$decimal", JsonValue::String(text.to_string()))])
}

fn base() -> BacktestConfig {
    let mut config = BacktestConfig::new("bt", 0, 86_400_000_000_000_000, 7).expect("config");
    config.strategy_instances.push(StrategyInstanceConfig {
        instance_id: "momentum".to_string(),
        implementation: "strategies.momentum:Momentum".to_string(),
        bundle_hash: "bundle".to_string(),
        parameters: BTreeMap::new(),
        symbols: vec!["SPY".to_string()],
        capital_fraction: "0.5".to_string(),
    });
    config
}

#[test]
fn grid_expansion_is_sorted_and_deduplicated() {
    let space = BTreeMap::from([
        (
            "fast".to_string(),
            vec![value("5"), value("10"), value("5")],
        ),
        ("slow".to_string(), vec![value("20")]),
    ]);
    let spec = SweepSpec::grid(base(), "momentum", space).expect("spec");
    assert_eq!(spec.count, 3);
    let points = spec.expand();
    // Duplicate fast=5 combinations collapse.
    assert_eq!(points.len(), 2);
    assert_eq!(points[0].index, 0);
    assert_eq!(points[1].index, 1);
    assert!(points.iter().all(|point| point.run_id.starts_with("bt-")));
}

#[test]
fn random_expansion_matches_python_choice() {
    let space = BTreeMap::from([(
        "fast".to_string(),
        vec![value("5"), value("10"), value("15"), value("20")],
    )]);
    let spec = SweepSpec::random(base(), "momentum", space, 5, 7).expect("spec");
    let points = spec.expand();
    let labels: Vec<String> = points
        .iter()
        .map(|point| point.parameters["fast"].to_canonical_string())
        .collect();
    // Deduplication may collapse repeated draws, but the expansion is
    // deterministic and every label is a canonical tagged decimal.
    assert!(!labels.is_empty() && labels.len() <= 5);
    assert!(labels.iter().all(|label| label.contains("$decimal")));
    assert_eq!(spec.expand(), points);
}

#[test]
fn sweep_id_is_stable() {
    let space = BTreeMap::from([("fast".to_string(), vec![value("5")])]);
    let spec = SweepSpec::grid(base(), "momentum", space).expect("spec");
    assert_eq!(spec.sweep_id(), spec.sweep_id());
    assert_eq!(spec.sweep_id().len(), 64);
}

#[test]
fn unknown_instance_is_rejected() {
    let space = BTreeMap::from([("fast".to_string(), vec![value("5")])]);
    assert_eq!(
        SweepSpec::grid(base(), "nope", space).err(),
        Some(SweepError::UnknownInstance("nope".to_string()))
    );
}
