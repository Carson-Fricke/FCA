use super::*;

#[test]
fn parses_backtest_subset() {
    let text = "shell: backtest\nseed: 1729\ninitial_cash: 100000.00\ndata:\n  symbols: [SPY]\n  bar_interval: 1min\nrisk_pipeline:\n  on_reject: reject_order\n  rules:\n    -\n      rule_id: max-order-notional\n      parameters:\n        limit: 10000.00\n";
    let value = load_yaml(text).expect("parse");
    let map = value.as_mapping().expect("mapping");
    assert_eq!(map["shell"].as_str(), Some("backtest"));
    assert_eq!(map["seed"].as_int(), Some(1729));
    assert_eq!(
        map["initial_cash"].as_decimal().map(|v| v.to_string()),
        Some("100000.00".to_string())
    );
    let data = map["data"].as_mapping().expect("data");
    assert_eq!(data["bar_interval"].as_str(), Some("1min"));
    let rules = map["risk_pipeline"].as_mapping().expect("rp")["rules"]
        .as_sequence()
        .expect("rules");
    assert_eq!(rules.len(), 1);
    assert_eq!(
        rules[0].as_mapping().expect("rule")["rule_id"].as_str(),
        Some("max-order-notional")
    );
}

#[test]
fn strips_comments_and_rejects_duplicates() {
    assert_eq!(
        load_yaml("a: 1 # comment\n")
            .expect("ok")
            .as_mapping()
            .unwrap()["a"]
            .as_int(),
        Some(1)
    );
    assert!(load_yaml("a: 1\na: 2\n").is_err());
}
