//! Bearer credential resolution for the harness MCP server.
//!
//! The MCP server is a client of the remote quant-harness API, so the one
//! credential it moves is that API's bearer token. The documented variable is
//! `HARNESS_API_BEARER_TOKEN`; the token is resolved per call and never
//! retained. The only serialized trace of the credential is the variable's
//! name plus a boolean `bearer_configured` flag.

use std::collections::BTreeMap;
use std::fmt;

/// Deployment variable holding the remote harness API bearer token.
pub const BEARER_TOKEN_VARIABLE: &str = "HARNESS_API_BEARER_TOKEN";

/// Raised when a remote call is attempted without a resolvable bearer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MissingBearerCredentialError;

impl fmt::Display for MissingBearerCredentialError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "a non-empty {BEARER_TOKEN_VARIABLE} is required for remote harness API calls"
        )
    }
}

impl std::error::Error for MissingBearerCredentialError {}

/// Credential surface the tool handlers compose against.
pub trait BearerTokenSource: Send + Sync {
    /// Resolve the current bearer token, or `None` when unconfigured.
    fn bearer_token(&self) -> Option<String>;
    /// Return the request headers, or fail when unconfigured.
    ///
    /// # Errors
    ///
    /// Returns [`MissingBearerCredentialError`] without a configured token.
    fn bearer_headers(&self) -> Result<BTreeMap<String, String>, MissingBearerCredentialError>;
}

/// Return the stripped bearer token from an environment mapping.
#[must_use]
pub fn bearer_token_from_environ(environ: Option<&BTreeMap<String, String>>) -> Option<String> {
    let token = match environ {
        Some(environ) => environ.get(BEARER_TOKEN_VARIABLE).cloned(),
        None => std::env::var(BEARER_TOKEN_VARIABLE).ok(),
    }?;
    let token = token.trim();
    if token.is_empty() {
        None
    } else {
        Some(token.to_string())
    }
}

/// Build the `Authorization` header for one remote harness API call.
///
/// # Errors
///
/// Returns [`MissingBearerCredentialError`] for a missing or blank token.
pub fn bearer_headers(
    token: Option<&str>,
) -> Result<BTreeMap<String, String>, MissingBearerCredentialError> {
    let token = token.map(str::trim).filter(|value| !value.is_empty());
    let Some(token) = token else {
        return Err(MissingBearerCredentialError);
    };
    let mut headers = BTreeMap::new();
    headers.insert("Authorization".to_string(), format!("Bearer {token}"));
    Ok(headers)
}

/// Resolve the bearer token from the deployment environment per call.
#[derive(Debug, Clone, Copy, Default)]
pub struct EnvironmentBearerTokenSource;

impl BearerTokenSource for EnvironmentBearerTokenSource {
    fn bearer_token(&self) -> Option<String> {
        bearer_token_from_environ(None)
    }

    fn bearer_headers(&self) -> Result<BTreeMap<String, String>, MissingBearerCredentialError> {
        bearer_headers(self.bearer_token().as_deref())
    }
}

#[cfg(test)]
mod tests {
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
}
