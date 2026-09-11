use super::*;

#[test]
fn converts_json_scalars_to_the_engine_model() {
    assert_eq!(json_to_yaml(&Value::Null), YamlValue::Null);
    assert_eq!(json_to_yaml(&Value::Bool(true)), YamlValue::Bool(true));
    assert_eq!(json_to_yaml(&serde_json::json!(4)), YamlValue::Int(4));
    assert_eq!(
        json_to_yaml(&serde_json::json!("0.05")),
        YamlValue::String("0.05".to_string())
    );
    assert!(matches!(
        json_to_yaml(&serde_json::json!(0.5)),
        YamlValue::Decimal(_)
    ));
}

#[test]
fn builds_a_native_configuration_from_a_submitted_document() {
    let document = serde_json::json!({
        "schema_version": 1,
        "shell": "backtest",
        "run_id": "run-1",
        "seed": 7,
        "strategy_instances": [{
            "instance_id": "spy-momentum",
            "implementation": "strategies.harness_examples.intraday_momentum:IntradayMomentumStrategy",
            "bundle_hash": "aaaa",
            "parameters": {"threshold": "0.05"}
        }]
    });
    let yaml = json_to_yaml(&document);
    let config = BacktestConfig::from_yaml_value(&yaml).expect("config");
    assert_eq!(config.run_id, "run-1");
    assert_eq!(config.seed, 7);
    assert_eq!(config.strategy_instances.len(), 1);
}
