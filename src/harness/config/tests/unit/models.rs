use super::*;
use crate::yaml::parse_yaml_mapping;

fn build(text: &str, shell: &str) -> Result<Value, ConfigError> {
    let mapping = parse_yaml_mapping(text).expect("yaml");
    match shell {
        "api" => build_api(&mapping),
        "backtest" => build_backtest(&mapping),
        "trader" => build_trader(&mapping),
        other => panic!("unknown shell {other}"),
    }
}

fn minimal_backtest() -> String {
    "schema_version: 1\nshell: backtest\nrun_id: r\nstart: \"2024-01-02T14:30:00Z\"\nend: \"2024-01-03T14:30:00Z\"\nclock: event_time\nevent_ordering: timestamp_then_sequence_then_class_then_feed\nseed: 1\nbase_currency: USD\ninitial_cash: 100.00\nstrategy_instances:\n  -\n    instance_id: s\n    implementation: x:Y\n    bundle_hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n    capital_fraction: 1.0\n    symbols: [SPY]\ndata:\n  snapshot_id: snap\n  fingerprint: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n  symbols: [SPY]\n  bar_interval: 1min\nrisk_pipeline:\n  on_reject: reject_order\n  rules:\n    -\n      rule_id: r\n      implementation: x:Y\nfees:\n  currency: USD\n  rounding: half_even\n  rules:\n    -\n      fee_id: f\n      basis: per_unit\n      rate: 0.01\n      minimum: 0.01\n      side: both\ndeployment:\n  name: d\n  target: local\n  environment: local\n  image: null\n  replicas: 1\n  restart_policy: never\n  network_access: disabled\n  resources:\n    cpu_cores: 1.0\n    memory_mib: 128\n".to_string()
}

#[test]
fn backtest_decimal_normalization_matches_python() {
    let value = build(&minimal_backtest(), "backtest").expect("build");
    assert_eq!(value["initial_cash"], json!({"$decimal": "100"}));
    assert_eq!(
        value["strategy_instances"][0]["capital_fraction"],
        json!({"$decimal": "1"})
    );
    assert_eq!(
        value["start"],
        json!({"$datetime": "2024-01-02T14:30:00.000000Z"})
    );
}

#[test]
fn backtest_rejects_network_enabled_deployment() {
    let text =
        minimal_backtest().replace("network_access: disabled", "network_access: runtime_only");
    assert!(build(&text, "backtest").is_err());
}

#[test]
fn unknown_fields_are_rejected() {
    let text = format!("{}unexpected: true\n", minimal_backtest());
    assert!(build(&text, "backtest").is_err());
}
