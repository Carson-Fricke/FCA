//! Configuration decimals reach an actual authored constructor as JSON numbers.
use backtest::backtest::config::BacktestConfig;
use harness_research::build_instances;
use harness_strategy_jit::ManagedWorkspace;

#[test]
fn configured_parameters_reach_authored_program_without_tags_or_precision_loss() {
    let directory = tempfile::tempdir().unwrap();
    let workspace = ManagedWorkspace::open(directory.path()).unwrap();
    let source = r#"
use harness_strategy_sdk::*;
struct Probe;
impl MarketConsumer for Probe {
    type Context = OrderSink;
    fn subscriptions(&self) -> Vec<Subscription> { vec![] }
}
impl StrategyProgram for Probe {
    fn new(p: serde_json::Value) -> Self {
        assert_eq!(p["integral_float"].as_f64(), Some(2.0));
        assert_eq!(p["fraction"].as_f64(), Some(1.2));
        assert_eq!(p["integer"].as_i64(), Some(2));
        assert_eq!(p["nested"]["values"][0].as_f64(), Some(1.3));
        assert_eq!(p["precise"].to_string(), "0.1234567890123456789012345678");
        Self
    }
    fn on_stop(&mut self, metrics: &mut MetricPublisher) { let _ = metrics.publish("constructed", 1.0); }
}
export_strategy_program!(Probe);
"#;
    let text = format!("shell: backtest\njob_id: parameter-probe\nstart: 2024-01-02T14:30:00Z\nend: 2024-01-02T15:00:00Z\ndata:\n  symbols: [SPY]\n  subscriptions:\n    - data_type: quote\n      symbols: [SPY]\nstrategy_instances:\n  - instance_id: probe\n    implementation: jit\n    bundle_hash: {}\n    symbols: [SPY]\n    capital_fraction: 1\n    parameters:\n      source: {}\n      integral_float: 2.0\n      fraction: 1.2\n      integer: 2\n      precise: 0.1234567890123456789012345678\n      nested:\n        values: [1.3]\n", harness_contracts::sha256::digest_hex(source.as_bytes()), serde_json::to_string(source).unwrap());
    let config = BacktestConfig::from_yaml(&text).unwrap();
    let mut instances = build_instances(&config, &workspace).unwrap();
    instances[0].runtime.on_stop().unwrap();
    let metrics = instances[0].runtime.published_metrics();
    assert_eq!(metrics["constructed"], "1");
}
