use super::*;

#[test]
fn manifest_hash_is_stable_and_domain_separated() {
    let value = JsonValue::object([("a", JsonValue::Int(1))]);
    let first = configuration_hash(&value);
    let second = configuration_hash(&value);
    assert_eq!(first, second);
    assert_eq!(first.len(), 64);
    assert_ne!(
        first,
        digest_hex(value.to_canonical_string().as_bytes()),
        "hash must be domain separated"
    );
}

#[test]
fn code_fingerprint_is_stable_across_calls() {
    assert_eq!(code_fingerprint(), code_fingerprint());
    assert_eq!(code_fingerprint().len(), 64);
}

#[test]
fn run_digest_orders_artifacts_by_name() {
    let mut left = std::collections::BTreeMap::new();
    left.insert("actions".to_string(), "aa".to_string());
    left.insert("fills".to_string(), "bb".to_string());
    let mut right = std::collections::BTreeMap::new();
    right.insert("fills".to_string(), "bb".to_string());
    right.insert("actions".to_string(), "aa".to_string());
    assert_eq!(run_digest("r", &left), run_digest("r", &right));
}
