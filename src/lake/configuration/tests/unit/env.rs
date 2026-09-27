use std::collections::BTreeMap;

use serde_json::json;

use super::*;

#[test]
fn lookup_is_case_insensitive() {
    let mut environ = BTreeMap::new();
    environ.insert("lake_provider__api_key".to_string(), "value".to_string());
    assert_eq!(
        lookup(&environ, "LAKE_PROVIDER__API_KEY"),
        Some("value".to_string())
    );
    assert_eq!(lookup(&environ, "OTHER"), None);
}

#[test]
fn apply_sets_nested_paths_and_parses_kinds() {
    let mut document = json!({"provider": {"request": {"max_concurrency": 4}}});
    let mut environ = BTreeMap::new();
    environ.insert("LAKE_PROVIDER__REQUEST__MAX_CONCURRENCY".into(), "8".into());
    environ.insert("LAKE_PROVIDER__NAME".into(), "alpaca".into());
    environ.insert(
        "LAKE_INGEST__BACKFILL__ASSET_PRIORITY".into(),
        "[\"a\", \"b\"]".into(),
    );
    environ.insert(
        "LAKE_STORAGE__PAUSE_INGEST_AT_CAPACITY".into(),
        "off".into(),
    );
    environ.insert("LAKE_UNKNOWN__KEY".into(), "ignored".into());
    apply(
        &mut document,
        &environ,
        &[
            ("provider.request.max_concurrency", EnvKind::Uint),
            ("provider.name", EnvKind::Str),
            ("ingest.backfill.asset_priority", EnvKind::StrList),
            ("storage.pause_ingest_at_capacity", EnvKind::Bool),
        ],
    )
    .expect("apply");
    assert_eq!(document["provider"]["request"]["max_concurrency"], 8);
    assert_eq!(document["provider"]["name"], "alpaca");
    assert_eq!(document["ingest"]["backfill"]["asset_priority"][1], "b");
    assert_eq!(document["storage"]["pause_ingest_at_capacity"], false);
    assert!(document.get("unknown").is_none());
}

#[test]
fn bad_scalars_are_rejected() {
    let mut document = json!({});
    let mut environ = BTreeMap::new();
    environ.insert(
        "LAKE_PROVIDER__REQUEST__MAX_CONCURRENCY".into(),
        "many".into(),
    );
    let error = apply(
        &mut document,
        &environ,
        &[("provider.request.max_concurrency", EnvKind::Uint)],
    )
    .expect_err("invalid");
    assert!(error.message.contains("must be a non-negative integer"));
}

#[test]
fn comma_lists_parse_without_brackets() {
    let mut document = json!({});
    let mut environ = BTreeMap::new();
    environ.insert("LAKE_LIST".into(), "a, b ,c".into());
    apply(&mut document, &environ, &[("list", EnvKind::StrList)]).expect("apply");
    assert_eq!(document["list"], json!(["a", "b", "c"]));
}
