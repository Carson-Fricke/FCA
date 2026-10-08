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
            "bd33134861bca6425d1ac3476b9b6a8358551a3f7d78129183f8168da00a4f09",
        ),
        (
            "api.yaml",
            "3b1a01c125bb246c965d877fdac68fb0d12a78941a89c20a4c62df508b51b676",
        ),
        (
            "trader.yaml",
            "11542adc1828c07c032542fbed14ab95a3b1d34ed2f94d37d1e39fe442273c35",
        ),
        (
            "trader-crypto.yaml",
            "2b2f18bc457ebb37a5a64eb81ea0e4c6f813e13a03ca8c337f15a3bc2003fbac",
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
        r#"{"base_currency":"USD","clock":"event_time","data":{"fingerprint":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","#,
        r#""snapshot_id":"market-lake-equities-2024-01-v1","#,
        r#""subscriptions":[{"data_type":"quote","symbols":["MSFT"]}],"symbols":["MSFT"]},"#,
        r#""deployment":{"environment":"local","image":null,"name":"backtest-job-local","#,
        r#""network_access":"disabled","replicas":1,"resources":{"cpu_cores":{"$decimal":"1"},"#,
        r#""memory_mib":1024},"restart_policy":"never","target":"local"},"#,
        r#""end":{"$datetime":"2024-01-04T21:00:00.000000Z"},"#,
        r#""event_ordering":"timestamp_then_sequence_then_class_then_feed","#,
        r#""fees":{"currency":"USD","rounding":"half_even","rules":[{"basis":"per_unit","#,
        r#""fee_id":"commission","minimum":{"$decimal":"0.01"},"rate":{"$decimal":"0.005"},"#,
        r#""side":"both"}]},"initial_cash":{"$decimal":"100000"},"#,
        r#""job_id":"job-momentum-msft-2024-01","risk_pipeline":{"on_reject":"reject_order","rules":[{"#,
        r#""implementation":"harness.risk.limits:MaxOrderNotional","#,
        r#""parameters":{"limit":{"$decimal":"10000"}},"rule_id":"max-order-notional"},"#,
        r#"{"implementation":"harness.risk.limits:MaxPositionNotional","#,
        r#""parameters":{"limit":{"$decimal":"50000"}},"rule_id":"max-position-notional"}]},"#,
        r#""schema_version":1,"seed":1729,"shell":"backtest","#,
        r#""start":{"$datetime":"2024-01-02T14:30:00.000000Z"},"strategy_instances":[{"#,
        r#""bundle_hash":"dca3e6dda63c83d8a9d7cc0688e180a42e3d3e0dd502c3d4d731db505c2d8998","#,
        r#""capital_fraction":{"$decimal":"1"},"#,
        r#""implementation":"jit","#,
        r#""instance_id":"msft-momentum","parameters":{"lookback":4,"quantity":20,"#,
        r#""source_path":"momentum.rs"},"symbols":["MSFT"]}]}"#
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

#[test]
fn subscription_snapshot_is_strict_and_canonical() {
    use config::{models::build_backtest, parse_yaml_mapping, YamlValue};
    use std::collections::BTreeMap;

    let text = std::fs::read_to_string(config_dir().join("backtest.yaml")).unwrap();
    let original = parse_yaml_mapping(&text).unwrap();
    let canonical = build_backtest(&original).unwrap();
    assert_eq!(
        canonical["data"]["subscriptions"],
        serde_json::json!([{"data_type": "quote", "symbols": ["MSFT"]}])
    );
    assert!(canonical["data"].get("bar_interval").is_none());
    let mut interval_only = original.clone();
    let YamlValue::Map(data) = interval_only.get_mut("data").unwrap() else {
        panic!("data mapping")
    };
    data.remove("subscriptions");
    data.insert("bar_interval".into(), YamlValue::Str("1min".into()));
    assert!(build_backtest(&interval_only).is_err());
    let subscription = |kind: &str, interval: Option<&str>| {
        let mut map = BTreeMap::from([
            ("data_type".into(), YamlValue::Str(kind.into())),
            (
                "symbols".into(),
                YamlValue::Seq(vec![YamlValue::Str("QQQ".into())]),
            ),
        ]);
        if let Some(interval) = interval {
            map.insert("interval".into(), YamlValue::Str(interval.into()));
        }
        YamlValue::Map(map)
    };
    let with_inputs = |inputs: Vec<YamlValue>| {
        let mut mapping = original.clone();
        let YamlValue::Map(data) = mapping.get_mut("data").unwrap() else {
            panic!("data mapping")
        };
        data.remove("bar_interval");
        data.insert("subscriptions".into(), YamlValue::Seq(inputs));
        mapping
    };
    let ticks = build_backtest(&with_inputs(vec![subscription("quote", None)])).unwrap();
    assert!(ticks["data"].get("bar_interval").is_none());
    assert_eq!(
        ticks["data"]["subscriptions"],
        serde_json::json!([{"data_type": "quote", "symbols": ["QQQ"]}])
    );
    let mixed = build_backtest(&with_inputs(vec![
        subscription("quote", None),
        subscription("bars", Some("1min")),
    ]))
    .unwrap();
    assert_eq!(mixed["data"]["subscriptions"][0]["interval"], "1min");
    for input in [
        subscription("bars", None),
        subscription("quote", Some("1min")),
        subscription("bars", Some("1m")),
        subscription("ticks", None),
    ] {
        assert!(build_backtest(&with_inputs(vec![input])).is_err());
    }
}
