use super::*;

const CANONICAL_HASH: &str = "ea59ca33c8c764b0b2304d3e3b0ae15cdb8c71f44bb37493274f347535d89d22";

fn repo_config_path() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("config")
        .join("harness")
        .join("backtest.yaml")
}

#[test]
fn canonical_configuration_hash_is_pinned() {
    // The committed benchmark configuration is content-pinned so accidental
    // edits are caught; the hash was re-pinned when the benchmark program
    // became a JIT-authored Rust source.
    let text = std::fs::read_to_string(repo_config_path()).expect("read config");
    let config = BacktestConfig::from_yaml(&text).expect("parse");
    assert_eq!(config.configuration_hash(), CANONICAL_HASH);
}

#[test]
fn normalized_decimal_matches_python_format() {
    assert_eq!(normalized_fixed("100000.00".parse().unwrap()), "100000");
    assert_eq!(normalized_fixed("1.0".parse().unwrap()), "1");
    assert_eq!(normalized_fixed("0.005".parse().unwrap()), "0.005");
    assert_eq!(normalized_fixed("0.01".parse().unwrap()), "0.01");
    assert_eq!(normalized_fixed("10000.00".parse().unwrap()), "10000");
}

#[test]
fn rejects_non_backtest_shell() {
    assert!(BacktestConfig::from_yaml("shell: api\n").is_err());
}
