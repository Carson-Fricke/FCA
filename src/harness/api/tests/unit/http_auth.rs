use super::*;

#[test]
fn bearer_matching_is_scheme_insensitive() {
    assert!(authenticate_bearer(Some("Bearer secret"), Some("secret")));
    assert!(authenticate_bearer(Some("bearer secret"), Some("secret")));
    assert!(!authenticate_bearer(Some("Basic secret"), Some("secret")));
    assert!(!authenticate_bearer(Some("Bearer wrong"), Some("secret")));
    assert!(!authenticate_bearer(None, Some("secret")));
}

#[test]
fn bearer_fails_closed_without_a_configured_token() {
    assert!(!authenticate_bearer(Some("Bearer anything"), None));
}

#[test]
fn blank_explicit_token_is_rejected() {
    assert_eq!(
        configured_bearer_token(Some("  ".to_string())),
        Err(AuthError::EmptyToken)
    );
    assert_eq!(
        configured_bearer_token(Some("value".to_string())),
        Ok(Some("value".to_string()))
    );
}
