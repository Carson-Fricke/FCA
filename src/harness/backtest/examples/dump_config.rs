use backtest::backtest::config::BacktestConfig;

fn main() {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("config")
        .join("harness")
        .join("backtest.yaml");
    let text = std::fs::read_to_string(path).expect("read");
    let config = BacktestConfig::from_yaml(&text).expect("parse");
    println!("{}", config.canonical_json().to_canonical_string());
    println!("HASH {}", config.configuration_hash());
}
