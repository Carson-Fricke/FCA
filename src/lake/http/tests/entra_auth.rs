//! Entra managed-identity JWT authentication, ported from
//! `tests/lake/http/test_entra_auth.py`. The JWKS fetch is a fake and the
//! test keys are generated in-process, so no network or Entra endpoint is
//! touched.

mod support;

use std::sync::Arc;

use axum::http::{header, StatusCode};
use futures_util::future::BoxFuture;
use lake_contracts::DataError;
use lake_http::auth::{
    authenticate_credential, looks_like_jwt, EntraJwtAuthenticator, JwksFetcher,
};
use rsa::pkcs1v15::SigningKey;
use rsa::signature::{SignatureEncoding, Signer};
use rsa::traits::PublicKeyParts;
use rsa::{RsaPrivateKey, RsaPublicKey};
use serde_json::{json, Value};
use sha2::Sha256;
use support::*;

const TENANT_ID: &str = "6c199962-2933-4a4b-901f-46f1578826c4";
const AUDIENCE: &str = "cbc572d2-ae2e-4501-af76-0515e2f73bcd";
const HARNESS_AUDIENCE: &str = "c380c885-4a51-4b0d-9dc7-2f0c2fc70705";
const ROLE: &str = "Lake.Access";
const V1_ISSUER: &str = "https://sts.windows.net/6c199962-2933-4a4b-901f-46f1578826c4/";
const V2_ISSUER: &str =
    "https://login.microsoftonline.com/6c199962-2933-4a4b-901f-46f1578826c4/v2.0";
const OTHER_TENANT_ISSUER: &str = "https://sts.windows.net/22222222-2222-2222-2222-222222222222/";
const KID: &str = "test-signing-key";

/// A JWKS fetch boundary serving one generated key without network access.
struct StaticJwks {
    jwks_json: String,
    urls_seen: std::sync::Mutex<Vec<String>>,
}

impl JwksFetcher for StaticJwks {
    fn fetch<'a>(&'a self, jwks_url: &'a str) -> BoxFuture<'a, Result<String, DataError>> {
        self.urls_seen
            .lock()
            .expect("lock")
            .push(jwks_url.to_string());
        let body = self.jwks_json.clone();
        Box::pin(async move { Ok(body) })
    }
}

fn b64url(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut encoded = String::new();
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = chunk.get(1).copied().unwrap_or(0) as u32;
        let b2 = chunk.get(2).copied().unwrap_or(0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        encoded.push(char::from(ALPHABET[(triple >> 18) as usize & 0x3f]));
        encoded.push(char::from(ALPHABET[(triple >> 12) as usize & 0x3f]));
        if chunk.len() > 1 {
            encoded.push(char::from(ALPHABET[(triple >> 6) as usize & 0x3f]));
        }
        if chunk.len() > 2 {
            encoded.push(char::from(ALPHABET[triple as usize & 0x3f]));
        }
    }
    encoded
}

/// Mint one RS256 token with a fixed header kid, like the Python `_mint`.
fn mint(
    private_key: &RsaPrivateKey,
    issuer: &str,
    audience: &str,
    roles: Option<Value>,
    claim_overrides: Value,
) -> String {
    let now = jsonwebtoken::get_current_timestamp() as i64;
    let mut claims = json!({
        "aud": audience,
        "iss": issuer,
        "sub": "00000000-0000-0000-0000-000000000000",
        "appid": "11111111-1111-1111-1111-111111111111",
        "roles": roles.unwrap_or_else(|| json!([ROLE])),
        "iat": now - 5,
        "nbf": now - 5,
        "exp": now + 3600,
    });
    if let Some(object) = claim_overrides.as_object() {
        for (key, value) in object {
            claims[key.as_str()] = value.clone();
        }
    }
    let header = json!({"alg": "RS256", "typ": "JWT", "kid": KID});
    let signing_input = format!(
        "{}.{}",
        b64url(header.to_string().as_bytes()),
        b64url(claims.to_string().as_bytes())
    );
    let signing_key = SigningKey::<Sha256>::new(private_key.clone());
    let signature = signing_key.sign(signing_input.as_bytes());
    format!("{signing_input}.{}", b64url(&signature.to_bytes()))
}

fn mint_default(private_key: &RsaPrivateKey) -> String {
    mint(private_key, V1_ISSUER, AUDIENCE, None, Value::Null)
}

fn public_jwk(private_key: &RsaPrivateKey) -> Value {
    let key = RsaPublicKey::from(private_key);
    json!({
        "kty": "RSA",
        "n": b64url(&key.n().to_bytes_be()),
        "e": b64url(&key.e().to_bytes_be()),
        "kid": KID,
        "use": "sig",
        "alg": "RS256",
    })
}

fn authenticator(private_key: &RsaPrivateKey) -> (Arc<StaticJwks>, EntraJwtAuthenticator) {
    authenticator_with_role(private_key, ROLE)
}

fn authenticator_with_role(
    private_key: &RsaPrivateKey,
    role: &str,
) -> (Arc<StaticJwks>, EntraJwtAuthenticator) {
    let fetcher = Arc::new(StaticJwks {
        jwks_json: json!({"keys": [public_jwk(private_key)]}).to_string(),
        urls_seen: std::sync::Mutex::new(Vec::new()),
    });
    let authenticator = EntraJwtAuthenticator::with_fetcher(
        TENANT_ID,
        AUDIENCE,
        role,
        Arc::clone(&fetcher) as Arc<dyn JwksFetcher>,
    )
    .expect("authenticator");
    (fetcher, authenticator)
}

#[test]
fn authenticator_rejects_blank_configuration() {
    let fetcher: Arc<dyn JwksFetcher> = Arc::new(StaticJwks {
        jwks_json: String::new(),
        urls_seen: std::sync::Mutex::new(Vec::new()),
    });
    let error = EntraJwtAuthenticator::with_fetcher("  ", AUDIENCE, ROLE, fetcher.clone())
        .expect_err("blank tenant");
    assert!(error.to_string().contains("tenant id"));
    let error = EntraJwtAuthenticator::with_fetcher(TENANT_ID, "", ROLE, fetcher.clone())
        .expect_err("blank audience");
    assert!(error.to_string().contains("audience"));
    let error = EntraJwtAuthenticator::with_fetcher(TENANT_ID, AUDIENCE, "", fetcher)
        .expect_err("blank role");
    assert!(error.to_string().contains("role"));
}

#[tokio::test]
async fn valid_v1_issuer_token_is_accepted() {
    let (_fetcher, authenticator) = authenticator(&test_key());
    let token = mint_default(&test_key());
    assert_eq!(
        authenticator.authenticate(&token).await.expect("accepted"),
        token
    );
}

#[tokio::test]
async fn valid_v2_issuer_token_is_accepted() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, authenticator) = authenticator(&private_key);
    let token = mint(&private_key, V2_ISSUER, AUDIENCE, None, Value::Null);
    assert_eq!(
        authenticator.authenticate(&token).await.expect("accepted"),
        token
    );
}

#[tokio::test]
async fn single_string_role_claim_is_accepted() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, authenticator) = authenticator(&private_key);
    let token = mint(
        &private_key,
        V1_ISSUER,
        AUDIENCE,
        Some(Value::from(ROLE)),
        json!({"appid": Value::Null}),
    );
    assert_eq!(
        authenticator.authenticate(&token).await.expect("accepted"),
        token
    );
}

#[tokio::test]
async fn wrong_audience_is_rejected() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, authenticator) = authenticator(&private_key);
    let token = mint(&private_key, V1_ISSUER, HARNESS_AUDIENCE, None, Value::Null);
    assert!(authenticator.authenticate(&token).await.is_err());
}

#[tokio::test]
async fn wrong_role_and_missing_role_are_rejected() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, authenticator) = authenticator(&private_key);

    let token = mint(
        &private_key,
        V1_ISSUER,
        AUDIENCE,
        Some(json!(["Harness.Access"])),
        Value::Null,
    );
    assert!(authenticator.authenticate(&token).await.is_err());

    let token = mint(
        &private_key,
        V1_ISSUER,
        AUDIENCE,
        Some(json!([])),
        Value::Null,
    );
    assert!(authenticator.authenticate(&token).await.is_err());
}

#[tokio::test]
async fn expired_token_is_rejected() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, authenticator) = authenticator(&private_key);
    let now = jsonwebtoken::get_current_timestamp() as i64;
    let token = mint(
        &private_key,
        V1_ISSUER,
        AUDIENCE,
        None,
        json!({"iat": now - 7200, "nbf": now - 7200, "exp": now - 300}),
    );
    assert!(authenticator.authenticate(&token).await.is_err());
}

#[tokio::test]
async fn issuer_from_another_tenant_is_rejected() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, authenticator) = authenticator(&private_key);
    let token = mint(
        &private_key,
        OTHER_TENANT_ISSUER,
        AUDIENCE,
        None,
        Value::Null,
    );
    assert!(authenticator.authenticate(&token).await.is_err());
}

#[tokio::test]
async fn tampered_signature_is_rejected() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, authenticator) = authenticator(&private_key);
    let token = mint_default(&private_key);
    let (header, payload, signature) = {
        let mut parts = token.split('.');
        let header = parts.next().expect("header").to_string();
        let payload = parts.next().expect("payload").to_string();
        let signature = parts.next().expect("signature").to_string();
        (header, payload, signature)
    };
    // Tamper a mid-string character: the final base64 character can carry
    // only padding bits, so flipping it may decode to the identical bytes.
    let middle = signature.len() / 2;
    let tampered = if signature.as_bytes()[middle] != b'A' {
        "A"
    } else {
        "B"
    };
    let token = format!(
        "{header}.{payload}.{}{tampered}{}",
        &signature[..middle],
        &signature[middle + 1..]
    );
    assert!(authenticator.authenticate(&token).await.is_err());
}

#[tokio::test]
async fn token_signed_by_another_key_is_rejected() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, authenticator) = authenticator(&private_key);
    let other_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let token = mint_default(&other_key);
    assert!(authenticator.authenticate(&token).await.is_err());
}

#[tokio::test]
async fn malformed_credential_is_rejected() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, authenticator) = authenticator(&private_key);
    assert!(authenticator.authenticate("not-a-token").await.is_err());
}

#[tokio::test]
async fn combined_authenticator_accepts_valid_entra_jwt_and_static_bearer() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, entra) = authenticator(&private_key);
    let token = mint_default(&private_key);

    assert!(
        authenticate_credential(
            Some(&format!("Bearer {token}")),
            Some("static-secret"),
            &entra
        )
        .await
    );
    assert!(
        authenticate_credential(Some("Bearer static-secret"), Some("static-secret"), &entra).await
    );
    assert!(
        !authenticate_credential(Some("Bearer aaa.bbb.ccc"), Some("static-secret"), &entra).await
    );
    assert!(!authenticate_credential(None, Some("static-secret"), &entra).await);
    assert!(
        !authenticate_credential(Some("Basic test-secret"), Some("static-secret"), &entra).await
    );
}

#[test]
fn jwt_shape_detection_requires_three_nonempty_segments() {
    assert!(looks_like_jwt("aaa.bbb.ccc"));
    assert!(!looks_like_jwt("not-a-token"));
    assert!(!looks_like_jwt("aaa.bbb"));
    assert!(!looks_like_jwt("aaa..ccc"));
}

#[tokio::test]
async fn app_accepts_valid_entra_jwt_without_static_token() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, entra) = authenticator(&private_key);
    let token = mint_default(&private_key);

    let mut state = base_state(Arc::new(lake_catalog::MemoryCatalogRepository::new()));
    state.bearer_token = None;
    state.entra = Some(Arc::new(entra));
    let router = build_app(state);

    let request = bearer_request(&token);
    let (status, _, body) = send(&router, request).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, json!({"status": "ok"}));
}

#[tokio::test]
async fn app_accepts_static_bearer_when_entra_configured() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, entra) = authenticator(&private_key);

    let mut state = base_state(Arc::new(lake_catalog::MemoryCatalogRepository::new()));
    state.bearer_token = Some("transition-secret".to_string());
    state.entra = Some(Arc::new(entra));
    let router = build_app_with(
        state,
        lake_http::LakeApiConfig {
            bearer_token: Some("transition-secret".to_string()),
            ensure_wait_deadline_seconds: 1.0,
        },
    );

    let (status, _, _) = send(&router, bearer_request("transition-secret")).await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn app_rejects_invalid_entra_credentials_with_the_stable_error() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, entra) = authenticator(&private_key);
    let now = jsonwebtoken::get_current_timestamp() as i64;

    let rejected = [
        mint(&private_key, V1_ISSUER, HARNESS_AUDIENCE, None, Value::Null),
        mint(
            &private_key,
            V1_ISSUER,
            AUDIENCE,
            Some(json!(["Harness.Access"])),
            Value::Null,
        ),
        mint(
            &private_key,
            V1_ISSUER,
            AUDIENCE,
            Some(json!([])),
            Value::Null,
        ),
        mint(
            &private_key,
            V1_ISSUER,
            AUDIENCE,
            None,
            json!({"iat": now - 7200, "nbf": now - 7200, "exp": now - 300}),
        ),
        mint(
            &private_key,
            OTHER_TENANT_ISSUER,
            AUDIENCE,
            None,
            Value::Null,
        ),
        "aaa.bbb.ccc".to_string(),
    ];

    let mut state = base_state(Arc::new(lake_catalog::MemoryCatalogRepository::new()));
    state.bearer_token = Some("transition-secret".to_string());
    state.entra = Some(Arc::new(entra));
    let router = build_app(state);

    for credential in rejected {
        let (status, headers, body) = send(&router, bearer_request(&credential)).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "{credential}");
        assert_eq!(headers["www-authenticate"], "Bearer");
        assert_eq!(
            body,
            json!({"error": {"code": "unauthorized", "message": "a valid bearer token is required"}})
        );
    }
}

#[tokio::test]
async fn app_health_stays_unauthenticated_when_entra_configured() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let (_fetcher, entra) = authenticator(&private_key);
    let mut state = base_state(Arc::new(lake_catalog::MemoryCatalogRepository::new()));
    state.bearer_token = Some("transition-secret".to_string());
    state.entra = Some(Arc::new(entra));
    let router = build_app(state);

    for path in ["/health", "/healthz"] {
        let (status, _, body) = send(&router, get(path)).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, json!({"status": "ok"}));
    }
}

#[tokio::test]
async fn app_without_entra_configuration_keeps_bearer_only() {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key");
    let router = build_app(base_state(Arc::new(
        lake_catalog::MemoryCatalogRepository::new(),
    )));

    let (status, _, _) = send(&router, bearer_request("test-secret")).await;
    assert_eq!(status, StatusCode::OK);

    let token = mint_default(&private_key);
    let (status, _, _) = send(&router, bearer_request(&token)).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

/// Build an authenticated GET with an explicit bearer credential.
fn bearer_request(credential: &str) -> axum::http::Request<axum::body::Body> {
    axum::http::Request::builder()
        .method("GET")
        .uri("/v1/auth/check")
        .header(header::AUTHORIZATION, format!("Bearer {credential}"))
        .body(axum::body::Body::empty())
        .expect("request")
}

/// Lazily-created shared test signing key.
fn test_key() -> RsaPrivateKey {
    use std::sync::OnceLock;
    static KEY: OnceLock<RsaPrivateKey> = OnceLock::new();
    KEY.get_or_init(|| RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key"))
        .clone()
}
