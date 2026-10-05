use config::{parse_yaml_mapping, replay::parse_document, YamlValue};

#[test]
fn sequence_mapping_forms_share_one_parser_and_reject_duplicate_keys() {
    let compact = "items:\n  - name: first\n    parameters:\n      value: 1.25\n  - name: second\n    parameters:\n      value: 2.5\n";
    let expanded = "items:\n  -\n    name: first\n    parameters:\n      value: 1.25\n  -\n    name: second\n    parameters:\n      value: 2.5\n";
    assert_eq!(
        parse_yaml_mapping(compact).unwrap(),
        parse_yaml_mapping(expanded).unwrap()
    );
    assert!(parse_yaml_mapping("items:\n  - name: first\n    name: second\n").is_err());
    assert!(parse_yaml_mapping("items:\n  - name: first\n   bad_indent: true\n").is_err());
}

#[test]
fn shell_lookup_is_top_level_not_text_scanning() {
    let document = parse_document(r#"{"nested":{"shell":"study"},"shell":"backtest"}"#).unwrap();
    assert_eq!(document["shell"], YamlValue::Str("backtest".into()));
    assert!(!parse_document(r#"{"nested":{"shell":"study"}}"#)
        .unwrap()
        .contains_key("shell"));
    assert!(parse_document("shell: backtest\nshell: study\n").is_err());
}
