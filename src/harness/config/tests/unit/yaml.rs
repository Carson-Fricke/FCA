use super::*;

#[test]
fn parses_block_sequences_and_flow_sequences() {
    let mapping = parse_yaml_mapping(
        "symbols: [SPY, QQQ]\nrules:\n  -\n    rule_id: a\n  -\n    rule_id: b\n",
    )
    .expect("parse");
    assert_eq!(
        mapping.get("symbols"),
        Some(&YamlValue::Seq(vec![
            YamlValue::Str("SPY".to_string()),
            YamlValue::Str("QQQ".to_string())
        ]))
    );
    let rules = match mapping.get("rules") {
        Some(YamlValue::Seq(items)) => items,
        other => panic!("expected seq, got {other:?}"),
    };
    assert_eq!(rules.len(), 2);
}

#[test]
fn rejects_duplicate_keys_and_tabs() {
    assert!(parse_yaml_mapping("a: 1\na: 2\n").is_err());
    assert!(parse_yaml_mapping("a:\n\tb: 1\n").is_err());
}

#[test]
fn exact_decimals_keep_their_source_text() {
    let mapping = parse_yaml_mapping("rate: 0.005\ncount: 20\n").expect("parse");
    assert_eq!(
        mapping.get("rate"),
        Some(&YamlValue::Decimal("0.005".to_string()))
    );
    assert_eq!(mapping.get("count"), Some(&YamlValue::Int(20)));
}
