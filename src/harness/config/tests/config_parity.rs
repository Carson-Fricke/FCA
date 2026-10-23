//! Configuration parity goldens.
//!
//! The golden hashes were originally produced by the retired Python
//! configuration loader and are now self-pinned: they prove the Rust loader,
//! strict models, decimal normalization, datetime formatting, and canonical
//! JSON stay stable across edits to the loader itself.

use std::path::Path;
use std::path::PathBuf;

use config::canonical_json;
use config::load_configuration;

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
fn null_block_is_optional_strict_and_hash_conditional() {
    use config::models::{build_api, build_backtest, build_trader};
    use config::{configuration_hash, parse_yaml_mapping, YamlValue};
    use std::collections::BTreeMap;

    let base = || {
        BTreeMap::from([
            ("shell".to_string(), YamlValue::Str("backtest".into())),
            ("job_id".to_string(), YamlValue::Str("null-block".into())),
            (
                "data".to_string(),
                YamlValue::Map(BTreeMap::from([
                    (
                        "symbols".to_string(),
                        YamlValue::Seq(vec![YamlValue::Str("SPY".into())]),
                    ),
                    (
                        "subscriptions".to_string(),
                        YamlValue::Seq(vec![YamlValue::Map(BTreeMap::from([
                            ("data_type".to_string(), YamlValue::Str("bars".into())),
                            (
                                "symbols".to_string(),
                                YamlValue::Seq(vec![YamlValue::Str("SPY".into())]),
                            ),
                            ("interval".to_string(), YamlValue::Str("1min".into())),
                        ]))]),
                    ),
                ])),
            ),
        ])
    };
    let null = |mode: &str, block_size: i64, seed: i64| {
        YamlValue::Map(BTreeMap::from([
            ("mode".to_string(), YamlValue::Str(mode.into())),
            ("block_size".to_string(), YamlValue::Int(block_size)),
            ("seed".to_string(), YamlValue::Int(seed)),
        ]))
    };
    let integer = |text: &str| YamlValue::Int(text.parse().unwrap());

    let without = build_backtest(&base()).expect("absent null");
    assert!(
        without.get("null").is_none(),
        "absent null stays absent: {without}"
    );
    let mut with = base();
    with.insert(
        "null".into(),
        YamlValue::Map(BTreeMap::from([
            ("mode".into(), YamlValue::Str("block_permutation".into())),
            ("block_size".into(), integer("3")),
            ("seed".into(), integer("7")),
        ])),
    );
    let mut ordinary = base();
    ordinary.insert("null".into(), null("permutation", 1, 7));
    let ordinary = build_backtest(&ordinary).expect("valid ordinary permutation");
    assert_eq!(
        ordinary["null"],
        serde_json::json!({"mode": "permutation", "block_size": 1, "seed": 7})
    );
    let accepted = build_backtest(&with).expect("valid null block");
    assert_eq!(
        accepted["null"],
        serde_json::json!({"mode": "block_permutation", "block_size": 3, "seed": 7})
    );
    assert_ne!(
        configuration_hash(&accepted),
        configuration_hash(&without),
        "the null block participates in the canonical hash"
    );
    assert!(config::canonical_json(&accepted).contains("\"null\""));
    assert!(!config::canonical_json(&without).contains("\"null\""));

    let mut study = with.clone();
    study.insert("shell".into(), YamlValue::Str("study".into()));
    assert!(build_backtest(&study).is_ok(), "studies accept null");
    assert_eq!(build_backtest(&study).unwrap()["null"]["seed"], 7);

    let trader_text = std::fs::read_to_string(config_dir().join("trader.yaml")).unwrap();
    let mut trader = parse_yaml_mapping(&trader_text).unwrap();
    trader.insert(
        "null".into(),
        YamlValue::Map(BTreeMap::from([
            ("mode".into(), YamlValue::Str("block_permutation".into())),
            ("block_size".into(), integer("3")),
            ("seed".into(), integer("7")),
        ])),
    );
    let error = build_trader(&trader).expect_err("traders reject null");
    assert!(
        error.0.contains("unexpected field \"null\""),
        "trader error: {}",
        error.0
    );
    let api_text = std::fs::read_to_string(config_dir().join("api.yaml")).unwrap();
    let mut api = parse_yaml_mapping(&api_text).unwrap();
    api.insert(
        "null".into(),
        YamlValue::Map(BTreeMap::from([(
            "mode".into(),
            YamlValue::Str("block_permutation".into()),
        )])),
    );
    assert!(
        build_api(&api).is_err(),
        "api must reject the replay-only null block"
    );

    for (label, value, expected) in [
        (
            "ordinary permutation block size",
            null("permutation", 3, 1),
            "permutation requires block_size=1",
        ),
        (
            "unknown mode",
            null("shuffle", 3, 1),
            "null.mode must be one of",
        ),
        (
            "missing mode",
            YamlValue::Map(BTreeMap::from([
                ("block_size".into(), integer("3")),
                ("seed".into(), integer("1")),
            ])),
            "Field required: null.mode",
        ),
        (
            "missing block_size",
            YamlValue::Map(BTreeMap::from([
                ("mode".into(), YamlValue::Str("block_permutation".into())),
                ("seed".into(), integer("1")),
            ])),
            "Field required: null.block_size",
        ),
        (
            "missing seed",
            YamlValue::Map(BTreeMap::from([
                ("mode".into(), YamlValue::Str("block_permutation".into())),
                ("block_size".into(), integer("3")),
            ])),
            "Field required: null.seed",
        ),
        (
            "zero block",
            null("block_permutation", 0, 1),
            "null.block_size must be between 1 and 10000",
        ),
        (
            "oversized block",
            null("block_permutation", 10001, 1),
            "null.block_size must be between 1 and 10000",
        ),
        (
            "negative seed",
            null("block_permutation", 3, -1),
            "null.seed must not be negative",
        ),
        (
            "non-integer block",
            YamlValue::Map(BTreeMap::from([
                ("mode".into(), YamlValue::Str("block_permutation".into())),
                ("block_size".into(), YamlValue::Str("3".into())),
                ("seed".into(), integer("1")),
            ])),
            "null.block_size must be an integer",
        ),
    ] {
        let mut document = base();
        document.insert("null".into(), value);
        let error = build_backtest(&document).expect_err(label);
        assert!(error.0.contains(expected), "{label}: {}", error.0);
    }

    let mut extra = base();
    extra.insert(
        "null".into(),
        YamlValue::Map(BTreeMap::from([
            ("mode".into(), YamlValue::Str("block_permutation".into())),
            ("block_size".into(), integer("3")),
            ("seed".into(), integer("1")),
            ("unknown".into(), integer("1")),
        ])),
    );
    let error = build_backtest(&extra).expect_err("unknown null field");
    assert!(error.0.contains("unexpected field"), "{}", error.0);

    let mut scalar = base();
    scalar.insert("null".into(), integer("1"));
    assert!(build_backtest(&scalar).is_err(), "null must be a mapping");
}

#[test]
fn subscription_snapshot_is_strict_and_legacy_serialization_is_unchanged() {
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
