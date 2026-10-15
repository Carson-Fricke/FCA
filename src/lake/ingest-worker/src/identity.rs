//! Explicit user-assigned ACI identity through IMDS; cached separately for each resource.
use azure_core::{
    auth::{AccessToken, TokenCredential},
    error::{Error, ErrorKind},
};
use std::collections::BTreeMap;

#[derive(Debug)]
pub struct ManagedIdentity {
    client_id: String,
    http: reqwest::Client,
    cache: tokio::sync::Mutex<BTreeMap<String, AccessToken>>,
}
impl ManagedIdentity {
    pub fn new(client_id: String) -> Result<Self, lake_contracts::DataError> {
        Ok(Self {
            client_id,
            http: reqwest::Client::builder()
                .no_proxy()
                .timeout(std::time::Duration::from_secs(10))
                .build()
                .map_err(lake_contracts::DataError::io)?,
            cache: Default::default(),
        })
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
            .get("http://169.254.169.254/metadata/identity/oauth2/token")
            .header("Metadata", "true")
            .query(&[
                ("api-version", "2018-02-01"),
                ("resource", resource),
                ("client_id", self.client_id.as_str()),
            ])
            .send()
            .await
            .map_err(|_| error("ACI managed identity request failed"))?;
        if !response.status().is_success() {
            return Err(error(format!(
                "ACI managed identity returned {}",
                response.status()
            )));
        }
        let value: Token = response
            .json()
            .await
            .map_err(|_| error("invalid ACI managed identity response"))?;
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
