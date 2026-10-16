//! Explicit user-assigned ACA identity; cached separately for each resource.
use azure_core::{
    auth::{AccessToken, TokenCredential},
    error::{Error, ErrorKind},
};
use std::collections::BTreeMap;

pub struct ManagedIdentity {
    client_id: String,
    endpoint: String,
    header: String,
    http: reqwest::Client,
    cache: tokio::sync::Mutex<BTreeMap<String, AccessToken>>,
}
impl ManagedIdentity {
    pub fn new(client_id: String) -> Result<Self, lake_contracts::DataError> {
        Ok(Self {
            client_id,
            endpoint: std::env::var("IDENTITY_ENDPOINT").map_err(|_| {
                lake_contracts::DataError::value("ACA identity endpoint is required")
            })?,
            header: std::env::var("IDENTITY_HEADER")
                .map_err(|_| lake_contracts::DataError::value("ACA identity header is required"))?,
            http: reqwest::Client::builder()
                .no_proxy()
                .timeout(std::time::Duration::from_secs(10))
                .build()
                .map_err(lake_contracts::DataError::io)?,
            cache: Default::default(),
        })
    }
}
impl std::fmt::Debug for ManagedIdentity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ManagedIdentity").finish_non_exhaustive()
    }
}
fn error(message: impl std::fmt::Display) -> Error {
    Error::message(ErrorKind::Credential, message.to_string())
}
#[async_trait::async_trait]
impl TokenCredential for ManagedIdentity {
    async fn get_token(&self, scopes: &[&str]) -> azure_core::Result<AccessToken> {
        if scopes.len() != 1 {
            return Err(error("one managed identity resource scope is required"));
        }
        let resource = scopes[0]
            .strip_suffix("/.default")
            .ok_or_else(|| error("invalid managed identity scope"))?;
        let mut cache = self.cache.lock().await;
        if let Some(token) = cache.get(resource) {
            if token.expires_on > time::OffsetDateTime::now_utc() + time::Duration::minutes(5) {
                return Ok(token.clone());
            }
        }
        #[derive(serde::Deserialize)]
        struct Token {
            access_token: String,
            expires_on: String,
        }
        let response = self
            .http
            .get(&self.endpoint)
            .header("X-IDENTITY-HEADER", &self.header)
            .query(&[
                ("api-version", "2019-08-01"),
                ("resource", resource),
                ("client_id", self.client_id.as_str()),
            ])
            .send()
            .await
            .map_err(|_| error("ACA managed identity request failed"))?;
        if !response.status().is_success() {
            return Err(error(format!(
                "ACA managed identity returned {}",
                response.status()
            )));
        }
        let value: Token = response
            .json()
            .await
            .map_err(|_| error("invalid ACA managed identity response"))?;
        let token = AccessToken::new(
            value.access_token,
            time::OffsetDateTime::from_unix_timestamp(value.expires_on.parse().map_err(error)?)
                .map_err(error)?,
        );
        cache.insert(resource.into(), token.clone());
        Ok(token)
    }
    async fn clear_cache(&self) -> azure_core::Result<()> {
        self.cache.lock().await.clear();
        Ok(())
    }
}
