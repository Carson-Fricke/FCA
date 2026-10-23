use api::http::configuration::configuration_hash;
use api::http::configuration::ConfigurationParser;
use api::http::configuration::StrictJsonConfigurationParser;
use backtest::backtest::config::BacktestConfig;
use serde_json::json;
use serde_json::Value;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;

static CONTRACT_TEMP: AtomicU64 = AtomicU64::new(0);

fn acceptance(document: &Value, expected: bool) {
    let text = document.to_string();
    assert_eq!(
        StrictJsonConfigurationParser.parse(text.as_bytes()).is_ok(),
        expected,
        "API: {text}"
    );
    assert_eq!(
        BacktestConfig::from_yaml(&text).is_ok(),
        expected,
        "engine: {text}"
    );
    assert_eq!(
        mcp::tools::arguments::validate_configuration(document, None).is_ok(),
        expected,
        "MCP: {text}"
    );
    assert_eq!(
        api::parse_configuration_shell(&text).as_deref(),
        document["shell"].as_str()
    );
    let path = std::env::temp_dir().join(format!(
        "harness-contract-{}-{}.json",
        std::process::id(),
        CONTRACT_TEMP.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::write(&path, &text).unwrap();
    let loaded = if document["shell"] == "study" {
        harness_research::configuration::load_study_config(&path)
    } else {
        harness_research::configuration::load_backtest_config(&path)
    };
    std::fs::remove_file(path).unwrap();
    assert_eq!(loaded.is_ok(), expected, "CLI loader: {text}");
}

#[test]
fn replay_acceptance_is_shared_at_real_boundaries() {
    let valid = json!({"shell":"study","job_id":"parity","initial_cash":123.5,
        "data":{"symbols":["SPY"],
            "subscriptions":[{"data_type":"quote","symbols":["SPY"]}]}});
    acceptance(&valid, true);
    let mut multi = valid.clone();
    multi["data"]["subscriptions"] = json!([
        {"data_type":"bars","symbols":["SPY"],"interval":"1min"},
        {"data_type":"bars","symbols":["SPY"],"interval":"5min"},
        {"data_type":"bars","symbols":["SPY"],"interval":"1h"},
        {"data_type":"bars","symbols":["SPY"],"interval":"1d"}
    ]);
    acceptance(&multi, true);
    for (field, value) in [
        ("seed", json!("wrong")),
        ("schema_version", json!(2)),
        ("initial_cash", json!(-1)),
        ("unexpected", json!(true)),
        ("fees", json!([])),
    ] {
        let mut bad = valid.clone();
        bad[field] = value;
        acceptance(&bad, false);
    }
    for input in [
        json!({"data_type":"ticks","symbols":["SPY"]}),
        json!({"data_type":"quote","symbols":["SPY"],"interval":null}),
        json!({"data_type":"bars","symbols":["SPY"],"interval":"1s"}),
    ] {
        let mut bad = valid.clone();
        bad["data"]["subscriptions"] = json!([input]);
        acceptance(&bad, false);
    }
    acceptance(
        &json!({"shell":"backtest","job_id":"missing-subscriptions"}),
        false,
    );
    acceptance(
        &json!({"shell":"backtest","job_id":"interval-only",
            "data":{"symbols":["SPY"],"bar_interval":"1min"}}),
        false,
    );
    acceptance(
        &json!({"shell":"backtest","job_id":"top-level-symbols","symbols":["SPY"],
            "data":{"symbols":["SPY"],
                "subscriptions":[{"data_type":"quote","symbols":["SPY"]}]}}),
        false,
    );
    acceptance(&json!({"shell":"study","job_id":"missing-interval"}), false);
}

#[test]
fn null_transform_is_replay_only_strict_and_hash_conditional() {
    let data = json!({"symbols": ["SPY"],
        "subscriptions": [{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}]});
    let valid = json!({"shell":"study","job_id":"null-block","data":data.clone(),
        "null":{"mode":"block_permutation","block_size":4,"seed":9}});
    acceptance(&valid, true);
    for (field, value) in [
        ("mode", json!("shuffle")),
        ("mode", json!(1)),
        ("block_size", json!(0)),
        ("block_size", json!(10001)),
        ("seed", json!(-1)),
        ("unknown", json!(true)),
    ] {
        let mut bad = valid.clone();
        bad["null"][field] = value;
        acceptance(&bad, false);
    }
    for field in ["mode", "block_size", "seed"] {
        let mut missing = valid.clone();
        missing["null"].as_object_mut().unwrap().remove(field);
        acceptance(&missing, false);
    }
    let without = json!({"shell":"study","job_id":"null-block","data":data.clone()});
    acceptance(&without, true);
    let present = config::replay::validate_document(&valid).unwrap();
    let absent = config::replay::validate_document(&without).unwrap();
    assert!(present.get("null").is_some());
    assert!(absent.get("null").is_none());
    assert_ne!(
        config::configuration_hash(&present),
        config::configuration_hash(&absent)
    );
}

#[test]
fn submitted_and_resolved_identities_are_deliberately_distinct() {
    let document = json!({"shell":"backtest","job_id":"identity",
        "data":{"snapshot_id":"caf\u{e9}","symbols":["SPY"],
            "subscriptions":[{"data_type":"quote","symbols":["SPY"]}]}});
    let parsed = StrictJsonConfigurationParser
        .parse(document.to_string().as_bytes())
        .unwrap();
    assert_eq!(
        parsed.configuration_digest(),
        config::configuration_hash(&document)
    );
    assert_eq!(parsed.configuration_digest(), configuration_hash(&document));
    let resolved = BacktestConfig::from_yaml(&document.to_string()).unwrap();
    assert_ne!(parsed.configuration_digest(), resolved.configuration_hash());
    assert_eq!(
        resolved.configuration_hash(),
        backtest::backtest::manifest::configuration_hash(&resolved.canonical_json())
    );
}

#[test]
fn numeric_parameters_and_normalized_symbols_agree_between_yaml_json_and_engine() {
    let document = json!({"shell":"backtest","job_id":"decimals","initial_cash":100.5,
        "data":{"symbols":["spy","QQQ","spy"],
            "subscriptions":[{"data_type":"quote","symbols":["spy","QQQ"]}]},
        "strategy_instances":[{"instance_id":"test","implementation":"jit",
            "bundle_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "capital_fraction":"1.00","symbols":["SPY"],"parameters":{"threshold":0.125}}]});
    let canonical = config::replay::validate_document(&document).unwrap();
    let engine = BacktestConfig::from_yaml(&document.to_string()).unwrap();
    assert_eq!(engine.canonical_json().to_serde_value(), canonical);
    let yaml = "shell: backtest\njob_id: decimals\ninitial_cash: 100.500\ndata:\n  symbols: [spy, QQQ, spy]\n  subscriptions:\n    - data_type: quote\n      symbols: [spy, QQQ]\nstrategy_instances:\n  - instance_id: test\n    implementation: jit\n    bundle_hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n    capital_fraction: 1.00\n    symbols: [SPY]\n    parameters:\n      threshold: 0.1250\n";
    assert_eq!(
        BacktestConfig::from_yaml(yaml)
            .unwrap()
            .configuration_hash(),
        engine.configuration_hash()
    );
}

#[test]
fn screening_schema_is_shared_by_api_mcp_and_cli() {
    let source = "// screening source\n";
    let digest = harness_contracts::sha256::digest_hex(source.as_bytes());
    let document = json!({"schema_version":1,"shell":"screen","job_id":"parity-screen",
        "name":"parity",
        "as_of":"2024-01-19","bar_intervals":["1d"],"lookback_days":20,"rank_metric":"close",
        "source":source,"bundle_hash":digest,
        "definition_id":format!("sha256:{}", "0".repeat(64)),"definition_hash":"0".repeat(64)});
    for invalid in [false, true] {
        let mut document = document.clone();
        if invalid {
            document["unknown"] = json!(true);
        }
        let text = document.to_string();
        assert_eq!(
            StrictJsonConfigurationParser.parse(text.as_bytes()).is_ok(),
            !invalid
        );
        assert_eq!(
            mcp::tools::arguments::validate_configuration(&document, None).is_ok(),
            !invalid
        );
        let path = std::env::temp_dir().join(format!(
            "harness-screen-contract-{}.json",
            std::process::id()
        ));
        std::fs::write(&path, text).unwrap();
        assert_eq!(
            harness_research::configuration::load_screen_config(&path).is_ok(),
            !invalid
        );
        std::fs::remove_file(path).unwrap();
    }
}

#[test]
fn nanosecond_bounds_match_typed_resolved_identity_after_api_validation() {
    let mut hashes = Vec::new();
    for end in [
        "2024-01-02T14:30:00.123456789Z",
        "2024-01-02T14:30:00.123456790Z",
    ] {
        let document = json!({"shell":"backtest","job_id":"nanos",
            "start":"2024-01-02T16:30:00.123456788+02:00","end":end,
            "data":{"symbols":["SPY"],
                "subscriptions":[{"data_type":"quote","symbols":["SPY"]}]}});
        let parsed = StrictJsonConfigurationParser
            .parse(document.to_string().as_bytes())
            .unwrap();
        let typed = config::replay::validate_document(parsed.document()).unwrap();
        let resolved = BacktestConfig::from_yaml(&parsed.document().to_string()).unwrap();
        assert_eq!(resolved.canonical_json().to_serde_value(), typed);
        // Compare resolved trees in the same persisted encoding, not raw submission identity.
        assert_eq!(
            resolved.configuration_hash(),
            config::configuration_hash(&typed)
        );
        hashes.push(resolved.configuration_hash());
    }
    assert_ne!(hashes[0], hashes[1]);
}
