use super::*;

#[test]
fn parses_nested_mappings_and_sequences() {
    let parsed = parse_yaml(
        "schema_version: 1\nprovider:\n  name: alpaca\n  request:\n    max_concurrency: 4\n    retry:\n      honor_retry_after: true\nsymbols:\n  - AAPL\n  - SPY\nempty: null\n",
    )
    .expect("parse");
    assert_eq!(parsed["schema_version"], 1);
    assert_eq!(parsed["provider"]["request"]["max_concurrency"], 4);
    assert_eq!(
        parsed["provider"]["request"]["retry"]["honor_retry_after"],
        true
    );
    assert_eq!(parsed["symbols"][1], "SPY");
    assert!(parsed["empty"].is_null());
}

#[test]
fn parses_flow_lists_quotes_and_comments() {
    let parsed = parse_yaml(
        "values: [a, 'b', 3]\nquoted: \"line\\nbreak\"\nsingle: 'it''s'\ncommented: 5 # trailing\n   # whole line\nhash: '# not a comment'\n",
    )
    .expect("parse");
    assert_eq!(parsed["values"][0], "a");
    assert_eq!(parsed["values"][1], "b");
    assert_eq!(parsed["values"][2], 3);
    assert_eq!(parsed["quoted"], "line\nbreak");
    assert_eq!(parsed["single"], "it's");
    assert_eq!(parsed["commented"], 5);
    assert_eq!(parsed["hash"], "# not a comment");
}

#[test]
fn rejects_duplicate_keys() {
    let error = parse_yaml("a: 1\na: 2\n").expect_err("duplicate");
    assert!(error.message.contains("duplicate YAML key"));
}

#[test]
fn rejects_tab_indentation() {
    let error = parse_yaml("a:\n\tb: 1\n").expect_err("tabs");
    assert!(error.message.contains("uses tabs for indentation"));
}

#[test]
fn rejects_anchors_and_flow_mappings() {
    assert!(parse_yaml("a: &anchor\n").is_err());
    assert!(parse_yaml("a: {b: 1}\n").is_err());
}

#[test]
fn rejects_secret_bearing_keys() {
    let error = parse_yaml("api_key: value\n").expect_err("secret");
    assert!(error.message.contains("secret-bearing YAML key"));
    assert!(parse_yaml("provider:\n  api_secret: value\n").is_err());
    assert!(parse_yaml("bearer_token: value\n").is_err());
    assert!(parse_yaml("nested:\n  access-key: value\n").is_err());
}

#[test]
fn non_secret_lookalikes_are_allowed() {
    let parsed = parse_yaml("asset_priority: [equity_backfill]\nkeystones: 3\n").expect("allowed");
    assert_eq!(parsed["keystones"], 3);
}

#[test]
fn unterminated_quotes_and_flow_collections_are_rejected() {
    assert!(parse_yaml("a: \"unterminated\n").is_err());
    assert!(parse_yaml("a: [1, 2\n").is_err());
}
