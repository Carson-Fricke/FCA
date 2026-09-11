use super::*;

#[test]
fn headers_require_a_token() {
    assert!(bearer_headers(None).is_err());
    assert!(bearer_headers(Some("  ")).is_err());
    let headers = bearer_headers(Some(" secret ")).expect("headers");
    assert_eq!(
        headers.get("Authorization").map(String::as_str),
        Some("Bearer secret")
    );
}

#[test]
fn environ_reads_the_documented_variable() {
    let mut environ = BTreeMap::new();
    environ.insert(BEARER_TOKEN_VARIABLE.to_string(), " abc ".to_string());
    assert_eq!(
        bearer_token_from_environ(Some(&environ)).as_deref(),
        Some("abc")
    );
    let empty: BTreeMap<String, String> = BTreeMap::new();
    assert_eq!(bearer_token_from_environ(Some(&empty)), None);
}
