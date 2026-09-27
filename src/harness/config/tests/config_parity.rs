//! Configuration parity goldens.
//!
//! The golden hashes were originally produced by the retired Python
//! configuration loader and are now self-pinned: they prove the Rust loader,
//! strict models, decimal normalization, datetime formatting, and canonical
//! JSON stay stable across edits to the loader itself.

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
fn existing_configuration_hashes_match_the_goldens() {
    let cases = [
        (
            "backtest.yaml",
            "ea59ca33c8c764b0b2304d3e3b0ae15cdb8c71f44bb37493274f347535d89d22",
        ),
        (
            "api.yaml",
            "3b1a01c125bb246c965d877fdac68fb0d12a78941a89c20a4c62df508b51b676",
        ),
        (
            "trader.yaml",
            "ee86c6031c6a6ab00c6539bc6f2f7419a0514c15cc3fbb171944d0344eeb15fe",
        ),
        (
            "trader-crypto.yaml",
            "e279b8f16aa3a4c78c673d9f2b06d8d92acec26daa4cb4a39be14b2f0f986c5d",
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
fn backtest_canonical_json_is_pinned() {
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
        r#""bundle_hash":"a5447b54e9ce7ae0d8be7d5c3271d70cca0fd815791b615beef86394d7296b02","#,
        r#""capital_fraction":{"$decimal":"1"},"#,
        r#""implementation":"jit","#,
        r#""instance_id":"spy-momentum","parameters":{"lookback":4,"quantity":20,"#,
        r#""source_path":"momentum.rs"},"symbols":["SPY"]}]}"#
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
