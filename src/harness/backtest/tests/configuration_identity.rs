use backtest::backtest::config::BacktestConfig;
use backtest::backtest::manifest::{configuration_timestamp, utc_timestamp};
use backtest::sweeps::{configuration_digest, configuration_json};
use std::collections::BTreeMap;

#[test]
fn sweep_retains_its_shipped_utf8_v1_identity_through_shared_hasher() {
    let config = BacktestConfig::from_yaml(
        "shell: backtest\njob_id: sweep\ndata:\n  symbols: [\"caf\u{e9}\"]\n  subscriptions:\n    - data_type: quote\n      symbols: [\"caf\u{e9}\"]\n",
    )
    .unwrap();
    let parameters = BTreeMap::new();
    let value = configuration_json(&config, "instance", Some(&parameters)).to_serde_value();
    assert_eq!(
        configuration_digest(&config, "instance", &parameters),
        harness_contracts::configuration_hash(&value, harness_contracts::HashEncoding::Utf8)
    );
    assert_ne!(
        configuration_digest(&config, "instance", &parameters),
        harness_contracts::configuration_hash(
            &value,
            harness_contracts::HashEncoding::HistoricalAscii
        )
    );
}

#[test]
fn adjacent_nanosecond_bounds_have_distinct_resolved_hashes() {
    let first = BacktestConfig::from_yaml("shell: backtest\njob_id: nanos\nstart: 2024-01-02T14:30:00.123456788Z\nend: 2024-01-02T14:30:00.123456789Z\ndata:\n  symbols: [SPY]\n  subscriptions:\n    - data_type: quote\n      symbols: [SPY]\n").unwrap();
    let mut second = first.clone();
    second.end_ns += 1;
    assert_ne!(first.configuration_hash(), second.configuration_hash());
    assert_eq!(
        first.canonical_json().to_serde_value()["end"]["$datetime"],
        "2024-01-02T14:30:00.123456789Z"
    );
    assert_eq!(
        second.canonical_json().to_serde_value()["end"]["$datetime"],
        "2024-01-02T14:30:00.123456790Z"
    );
}

#[test]
fn configuration_precision_does_not_change_microsecond_or_artifact_formatting() {
    for timestamp in [0, 123_000_000, 123_456_000, -1_000, -123_456_000] {
        assert_eq!(
            configuration_timestamp(timestamp),
            utc_timestamp(timestamp, true)
        );
    }
    assert_eq!(
        configuration_timestamp(123_000_000),
        "1970-01-01T00:00:00.123000Z"
    );
    assert_eq!(
        configuration_timestamp(-1),
        "1969-12-31T23:59:59.999999999Z"
    );
    assert_eq!(
        utc_timestamp(123_456_789, true),
        "1970-01-01T00:00:00.123456Z"
    );
    assert_eq!(
        utc_timestamp(123_456_789, false),
        "1970-01-01T00:00:00.123456+00:00"
    );
}
