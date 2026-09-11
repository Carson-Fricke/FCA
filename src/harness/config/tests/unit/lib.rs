use super::*;
use serde_json::json;

#[test]
fn canonical_json_sorts_keys() {
    let value = json!({"b": 1, "a": {"d": 2, "c": 3}});
    assert_eq!(canonical_json(&value), r#"{"a":{"c":3,"d":2},"b":1}"#);
}

#[test]
fn hash_is_domain_separated() {
    let value = json!({"shell": "backtest"});
    let digest = configuration_hash(&value);
    assert_eq!(digest.len(), 64);
    assert_eq!(digest, configuration_hash(&value));
}
