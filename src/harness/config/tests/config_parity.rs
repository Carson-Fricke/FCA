//! Configuration parity against the Python implementation.
//!
//! The golden hashes were produced by
//! `harness.configuration.configuration_hash(load_configuration(path))` on the
//! repo's Python 3.12 environment. Matching them proves the Rust loader,
//! strict models, decimal normalization, datetime formatting, and canonical
//! JSON all reproduce Python byte-for-byte.

use std::path::{Path, PathBuf};

use config::{canonical_json, load_configuration};

fn config_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("config")
        .join("harness")
}

#[test]
fn existing_configuration_hashes_match_python() {
    let cases = [
        (
            "backtest.yaml",
            "9076d70fa89c3624ce292824665f45f5c6e5afa74837f92084ee7d1bd629adf5",
        ),
        (
            "api.yaml",
            "3b1a01c125bb246c965d877fdac68fb0d12a78941a89c20a4c62df508b51b676",
        ),
        (
            "trader.yaml",
            "7774d9a90413bb24a2ce1c599443811d483d986e3847637e3975c1e71a9d466b",
        ),
        (
            "trader-crypto.yaml",
            "19fde4cac40ba5200b73bdd71c53d42fee0d730bf4c13efa4735391e05c9d71e",
        ),
    ];
    for (name, expected) in cases {
        let path = config_dir().join(name);
        let configuration =
            load_configuration(&path).unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(
            configuration.configuration_hash(),
            expected,
            "hash mismatch for {name}:\n{}",
            configuration.canonical_json()
        );
    }
}

#[test]
fn backtest_canonical_json_matches_python() {
    let path = config_dir().join("backtest.yaml");
    let configuration = load_configuration(&path).expect("load backtest");
    let expected = concat!(
        r#"{"base_currency":"USD","clock":"event_time","data":{"bar_interval":"1min","#,
        r#""fingerprint":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","#,
        r#""snapshot_id":"market-lake-equities-2024-01-v1","symbols":["SPY"]},"#,
        r#""deployment":{"environment":"local","image":null,"name":"backtest-job-local","#,
        r#""network_access":"disabled","replicas":1,"resources":{"cpu_cores":{"$decimal":"1"},"#,
        r#""memory_mib":1024},"restart_policy":"never","target":"local"},"#,
        r#""end":{"$datetime":"2024-01-19T21:00:00.000000Z"},"#,
        r#""event_ordering":"timestamp_then_sequence_then_class_then_feed","#,
        r#""fees":{"currency":"USD","rounding":"half_even","rules":[{"basis":"per_unit","#,
        r#""fee_id":"commission","minimum":{"$decimal":"0.01"},"rate":{"$decimal":"0.005"},"#,
        r#""side":"both"}]},"initial_cash":{"$decimal":"100000"},"#,
        r#""risk_pipeline":{"on_reject":"reject_order","rules":[{"#,
        r#""implementation":"harness.risk.limits:MaxOrderNotional","#,
        r#""parameters":{"limit":{"$decimal":"10000"}},"rule_id":"max-order-notional"},"#,
        r#"{"implementation":"harness.risk.limits:MaxPositionNotional","#,
        r#""parameters":{"limit":{"$decimal":"50000"}},"rule_id":"max-position-notional"}]},"#,
        r#""run_id":"momentum-spy-2024-01","schema_version":1,"seed":1729,"shell":"backtest","#,
        r#""start":{"$datetime":"2024-01-02T14:30:00.000000Z"},"strategy_instances":[{"#,
        r#""bundle_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","#,
        r#""capital_fraction":{"$decimal":"1"},"#,
        r#""implementation":"strategies.harness_examples.intraday_momentum:IntradayMomentumStrategy","#,
        r#""instance_id":"spy-momentum","parameters":{"liquidation_buffer_minutes":40,"#,
        r#""lookback":4,"order_prefix":"mom-spy","quantity":20,"session_close_times":["#,
        r#""2024-01-02T19:30:00Z","2024-01-03T19:30:00Z","2024-01-04T19:30:00Z","#,
        r#""2024-01-05T19:30:00Z","2024-01-08T19:30:00Z","2024-01-09T19:30:00Z","#,
        r#""2024-01-10T19:30:00Z","2024-01-11T19:30:00Z","2024-01-12T19:30:00Z","#,
        r#""2024-01-15T19:30:00Z","2024-01-16T19:30:00Z","2024-01-17T19:30:00Z","#,
        r#""2024-01-18T19:30:00Z","2024-01-19T19:30:00Z"],"threshold":"0.05"},"symbols":["SPY"]}]}"#,
    );
    assert_eq!(canonical_json(configuration.canonical_value()), expected);
}

#[test]
fn validation_rejects_malformed_documents() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("tests")
        .join("harness")
        .join("configuration")
        .join("fixtures");
    for fixture in [
        "invalid-api.yaml",
        "invalid-backtest.yaml",
        "invalid-secret.yaml",
    ] {
        let path = dir.join(fixture);
        if path.exists() {
            assert!(
                load_configuration(&path).is_err(),
                "{fixture} should not validate"
            );
        }
    }
}
