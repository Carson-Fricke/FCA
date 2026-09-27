//! Bearer authentication for the market-lake HTTP boundary.
//!
//! Mirrors `lake.http.auth`: the static token is compared in constant time, a
//! missing or non-bearer credential fails closed, and every rejection renders
//! the uniform 401 envelope with `WWW-Authenticate: Bearer`. Entra managed-
//! identity JWTs validate against the tenant JWKS discovery document with both
//! v1 and v2 issuer forms, the configured audience, and the configured
//! application role; a JWT-shaped credential fails closed while any other
//! bearer string continues to validate against the static token.

use std::sync::Arc;

use axum::body::Body;
use axum::extract::State;
use axum::http::{header, Request};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use futures_util::future::BoxFuture;
use jsonwebtoken::jwk::JwkSet;
use jsonwebtoken::{decode, decode_header, Algorithm, DecodingKey, Validation};
use lake_contracts::DataError;
use serde_json::Value;

use crate::error::ApiError;
use crate::state::ApiContext;

const V1_ISSUER_TEMPLATE: &str = "https://sts.windows.net/{tenant}/";
const V2_ISSUER_TEMPLATE: &str = "https://login.microsoftonline.com/{tenant}/v2.0";
const JWKS_URL_TEMPLATE: &str = "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys";
const DEFAULT_ENTRA_ROLE: &str = "Lake.Access";

/// Compare two byte strings without early exit on content.
#[must_use]
pub fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    let mut difference = left.len() ^ right.len();
    let length = left.len().max(right.len());
    for index in 0..length {
        let a = left.get(index).copied().unwrap_or(0);
        let b = right.get(index).copied().unwrap_or(0);
        difference |= usize::from(a ^ b);
    }
    difference == 0
}

/// Validate one presented `Authorization` header against the configured token.
///
/// Returns whether the credential is acceptable. A missing header, a non-bearer
/// scheme, or an absent configured token always fails.
#[must_use]
pub fn authenticate_bearer(header_value: Option<&str>, expected_token: Option<&str>) -> bool {
    let Some(header_value) = header_value else {
        return false;
    };
    let Some((scheme, credential)) = header_value.split_once(' ') else {
        return false;
    };
    if !scheme.eq_ignore_ascii_case("bearer") {
        return false;
    }
    match expected_token {
        Some(expected) => constant_time_eq(credential.as_bytes(), expected.as_bytes()),
        None => false,
    }
}

/// Axum middleware requiring a valid bearer credential on protected routes.
///
/// Without Entra configuration this behaves exactly like the static bearer
/// check. With Entra configuration a three-segment JWT credential is validated
/// as an Entra token while any other bearer string continues to validate
/// against the static token; invalid Entra tokens fail closed with the shared
/// 401 error shape.
pub(crate) async fn require_bearer(
    State(context): State<ApiContext>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let header_value = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    let authorized = match &context.entra {
        Some(entra) => {
            authenticate_credential(header_value, context.bearer_token.as_deref(), entra).await
        }
        None => authenticate_bearer(header_value, context.bearer_token.as_deref()),
    };
    if authorized {
        next.run(request).await
    } else {
        ApiError::unauthorized().into_response()
    }
}

/// Whether a credential has the three-segment JWT shape.
#[must_use]
pub fn looks_like_jwt(credential: &str) -> bool {
    let segments: Vec<&str> = credential.split('.').collect();
    segments.len() == 3 && segments.iter().all(|segment| !segment.is_empty())
}

/// Validate one presented `Authorization` header with Entra JWTs enabled.
///
/// JWT-shaped credentials validate against the Entra authenticator; every
/// other bearer string falls back to the static token comparison.
pub async fn authenticate_credential(
    header_value: Option<&str>,
    expected_token: Option<&str>,
    entra: &EntraJwtAuthenticator,
) -> bool {
    let Some(header_value) = header_value else {
        return false;
    };
    let Some((scheme, credential)) = header_value.split_once(' ') else {
        return false;
    };
    if !scheme.eq_ignore_ascii_case("bearer") {
        return false;
    }
    if looks_like_jwt(credential) {
        return entra.authenticate(credential).await.is_ok();
    }
    match expected_token {
        Some(expected) => constant_time_eq(credential.as_bytes(), expected.as_bytes()),
        None => false,
    }
}

/// Fetch boundary for one tenant JWKS discovery document.
///
/// The real implementation performs the HTTPS discovery GET; tests inject a
/// static key set without network access, exactly like the Python suite's
/// `_StaticJwksClient`.
pub trait JwksFetcher: Send + Sync {
    /// Return the raw JWKS document body for `jwks_url`.
    fn fetch<'a>(&'a self, jwks_url: &'a str) -> BoxFuture<'a, Result<String, DataError>>;
}

/// Production fetcher over HTTPS.
#[derive(Debug, Default)]
pub struct HttpJwksFetcher {
    client: reqwest::Client,
}

impl JwksFetcher for HttpJwksFetcher {
    fn fetch<'a>(&'a self, jwks_url: &'a str) -> BoxFuture<'a, Result<String, DataError>> {
        Box::pin(async move {
            let response = self
                .client
                .get(jwks_url)
                .send()
                .await
                .map_err(|error| DataError::Io(error.to_string()))?;
            response
                .text()
                .await
                .map_err(|error| DataError::Io(error.to_string()))
        })
    }
}

/// Validates Entra managed-identity JWTs for the configured application.
///
/// Signature verification uses the tenant JWKS discovery document, the
/// audience must equal the lake application client ID, both the v1
/// `sts.windows.net` issuer form (minted for managed identities) and the v2
/// `login.microsoftonline.com` form are accepted, and the configured
/// application role must appear in the `roles` claim.
pub struct EntraJwtAuthenticator {
    tenant_id: String,
    audience: String,
    role: String,
    jwks_url: String,
    fetcher: Arc<dyn JwksFetcher>,
}

impl std::fmt::Debug for EntraJwtAuthenticator {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("EntraJwtAuthenticator")
            .field("tenant_id", &self.tenant_id)
            .field("audience", &self.audience)
            .field("role", &self.role)
            .finish()
    }
}

impl EntraJwtAuthenticator {
    /// Build the validator with the default role and HTTPS discovery fetcher.
    pub fn new(tenant_id: &str, audience: &str) -> Result<Self, DataError> {
        Self::build(
            tenant_id,
            audience,
            DEFAULT_ENTRA_ROLE,
            Arc::new(HttpJwksFetcher::default()),
        )
    }

    /// Build the validator with an explicit role.
    pub fn with_role(tenant_id: &str, audience: &str, role: &str) -> Result<Self, DataError> {
        Self::build(
            tenant_id,
            audience,
            role,
            Arc::new(HttpJwksFetcher::default()),
        )
    }

    /// Build the validator over an injected JWKS fetcher.
    pub fn with_fetcher(
        tenant_id: &str,
        audience: &str,
        role: &str,
        fetcher: Arc<dyn JwksFetcher>,
    ) -> Result<Self, DataError> {
        Self::build(tenant_id, audience, role, fetcher)
    }

    fn build(
        tenant_id: &str,
        audience: &str,
        role: &str,
        fetcher: Arc<dyn JwksFetcher>,
    ) -> Result<Self, DataError> {
        if tenant_id.trim().is_empty() {
            return Err(DataError::value(
                "entra tenant id must be a non-empty string",
            ));
        }
        if audience.trim().is_empty() {
            return Err(DataError::value(
                "entra audience must be a non-empty string",
            ));
        }
        if role.trim().is_empty() {
            return Err(DataError::value("entra role must be a non-empty string"));
        }
        Ok(Self {
            tenant_id: tenant_id.to_string(),
            audience: audience.to_string(),
            role: role.to_string(),
            jwks_url: JWKS_URL_TEMPLATE.replace("{tenant}", tenant_id),
            fetcher,
        })
    }

    /// The v1 and v2 issuers accepted for the configured tenant.
    #[must_use]
    pub fn issuers(&self) -> [String; 2] {
        [
            V1_ISSUER_TEMPLATE.replace("{tenant}", &self.tenant_id),
            V2_ISSUER_TEMPLATE.replace("{tenant}", &self.tenant_id),
        ]
    }

    /// Return the presented token after Entra validation, or a uniform 401.
    pub async fn authenticate(&self, token: &str) -> Result<String, ApiError> {
        self.try_authenticate(token)
            .await
            .map_err(|_| ApiError::unauthorized())?;
        Ok(token.to_string())
    }

    /// Validate one token, reporting the specific verification failure.
    ///
    /// Any failure fails closed; the HTTP boundary renders the uniform 401
    /// regardless of the underlying reason, matching the Python catch-all.
    async fn try_authenticate(&self, token: &str) -> Result<(), DataError> {
        let header = decode_header(token)
            .map_err(|error| DataError::value(format!("malformed token: {error}")))?;
        let kid = header
            .kid
            .ok_or_else(|| DataError::value("token header has no kid"))?;
        let body = self.fetcher.fetch(&self.jwks_url).await?;
        let jwks: JwkSet =
            serde_json::from_str(&body).map_err(|error| DataError::value(error.to_string()))?;
        let jwk = jwks
            .find(&kid)
            .ok_or_else(|| DataError::value(format!("no signing key for kid {kid:?}")))?;
        let key =
            DecodingKey::from_jwk(jwk).map_err(|error| DataError::value(error.to_string()))?;
        let issuers = self.issuers();
        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_audience(&[self.audience.as_str()]);
        validation.set_issuer(&issuers);
        validation.leeway = 0;
        validation.validate_nbf = true;
        let claims = decode::<Value>(token, &key, &validation)
            .map_err(|error| DataError::value(format!("token rejected: {error}")))?
            .claims;
        if !claim_has_role(claims.get("roles"), &self.role) {
            return Err(DataError::value(format!(
                "token is missing the {:?} role",
                self.role
            )));
        }
        Ok(())
    }
}

/// Whether the `roles` claim carries the required application role.
fn claim_has_role(roles: Option<&Value>, role: &str) -> bool {
    match roles {
        Some(Value::String(text)) => text == role,
        Some(Value::Array(items)) => items.iter().any(|item| item.as_str() == Some(role)),
        _ => false,
    }
}
