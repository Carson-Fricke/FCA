//! ACI control-plane adapter. A group name is derived solely from the retained job ID.
use crate::store::{io, success};
use azure_core::auth::TokenCredential;
use lake_contracts::DataError;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, sync::Arc};

#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AciConfig {
    pub subscription: String,
    pub resource_group: String,
    pub location: String,
    pub image: String,
    pub identity: String,
    pub identity_client_id: String,
    pub registry: String,
    pub cpu: f64,
    pub memory_gb: f64,
    pub max_workers: usize,
    pub timeout_seconds: u64,
    pub environment: BTreeMap<String, String>,
}

#[cfg(test)]
#[path = "../tests/unit/azure_http.rs"]
mod tests;
impl AciConfig {
    pub fn from_environment(env: &BTreeMap<String, String>) -> Result<Self, DataError> {
        let required = |key: &str| {
            env.get(key)
                .filter(|s| !s.trim().is_empty())
                .cloned()
                .ok_or_else(|| DataError::value(format!("{key} is required")))
        };
        let number = |key: &str, default: f64| -> Result<f64, DataError> {
            env.get(key)
                .map(|v| v.parse().map_err(io))
                .unwrap_or(Ok(default))
        };
        let cpu = number("LAKE_INGEST_CPU", 4.0)?;
        let memory_gb = number("LAKE_INGEST_MEMORY_GB", 8.0)?;
        let max_workers = number("LAKE_INGEST_MAX_WORKERS", 2.0)?;
        let timeout_seconds = number("LAKE_INGEST_TIMEOUT_SECONDS", 86400.0)?;
        if !cpu.is_finite()
            || cpu <= 0.0
            || !memory_gb.is_finite()
            || memory_gb <= 0.0
            || max_workers.fract() != 0.0
            || !(1.0..=16.0).contains(&max_workers)
            || timeout_seconds.fract() != 0.0
            || !(60.0..=604800.0).contains(&timeout_seconds)
        {
            return Err(DataError::value("invalid ingestion resource budget"));
        }
        let image = required("LAKE_INGEST_IMAGE")?;
        if !image.rsplit_once("@sha256:").is_some_and(|(_, digest)| {
            digest.len() == 64
                && digest
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        }) {
            return Err(DataError::value("LAKE_INGEST_IMAGE must be digest pinned"));
        }
        let environment = env
            .iter()
            .filter(|(key, _)| {
                matches!(
                    key.as_str(),
                    "LAKE_INGEST_SESSION_CONCURRENCY"
                        | "LAKE_STORAGE_ACCOUNT_NAME"
                        | "LAKE_STORAGE_CONTAINER"
                        | "LAKE_COVERAGE_TABLE"
                        | "LAKE_PROVIDER_KEY_VAULT_URL"
                        | "LAKE_PROVIDER__REQUEST__MAX_CONCURRENCY"
                        | "LAKE_PROVIDER__REQUEST__REQUESTS_PER_MINUTE"
                        | "LAKE_PROVIDER__REQUEST__TIMEOUT_SECONDS"
                        | "LAKE_PROVIDER__REQUEST__RETRY__MAX_ATTEMPTS"
                        | "LAKE_PROVIDER__REQUEST__RETRY__HONOR_RETRY_AFTER"
                )
            })
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        Ok(Self {
            subscription: required("AZURE_SUBSCRIPTION_ID")?,
            resource_group: required("LAKE_INGEST_RESOURCE_GROUP")?,
            location: required("LAKE_INGEST_LOCATION")?,
            image,
            identity: required("LAKE_INGEST_IDENTITY_ID")?,
            identity_client_id: required("LAKE_INGEST_IDENTITY_CLIENT_ID")?,
            registry: required("LAKE_INGEST_REGISTRY")?,
            cpu,
            memory_gb,
            max_workers: max_workers as usize,
            timeout_seconds: timeout_seconds as u64,
            environment,
        })
    }
}
#[derive(Debug, PartialEq, Eq)]
pub enum GroupState {
    Absent,
    Active,
    Stopped,
}
#[derive(Clone)]
pub struct AciRunner {
    pub config: AciConfig,
    credential: Arc<dyn TokenCredential>,
    http: reqwest::Client,
    endpoint: String,
}
impl AciRunner {
    pub fn for_execution(&self, config: AciConfig) -> Self {
        Self {
            config,
            ..self.clone()
        }
    }
    pub fn new(config: AciConfig, credential: Arc<dyn TokenCredential>) -> Result<Self, DataError> {
        Ok(Self {
            config,
            credential,
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()
                .map_err(io)?,
            endpoint: "https://management.azure.com".into(),
        })
    }
    pub fn group_name(id: &str) -> String {
        format!(
            "ingest-{}",
            &hex::encode(Sha256::digest(id.as_bytes()))[..48]
        )
    }
    fn url(&self, id: &str) -> String {
        format!("{}/subscriptions/{}/resourceGroups/{}/providers/Microsoft.ContainerInstance/containerGroups/{}?api-version=2023-05-01",self.endpoint,self.config.subscription,self.config.resource_group,Self::group_name(id))
    }
    async fn request(
        &self,
        method: reqwest::Method,
        id: &str,
    ) -> Result<reqwest::RequestBuilder, DataError> {
        let token = self
            .credential
            .get_token(&["https://management.azure.com/.default"])
            .await
            .map_err(io)?;
        Ok(self
            .http
            .request(method, self.url(id))
            .bearer_auth(token.token.secret()))
    }
    pub async fn inspect(&self, id: &str) -> Result<GroupState, DataError> {
        let response = self
            .request(reqwest::Method::GET, id)
            .await?
            .send()
            .await
            .map_err(io)?;
        if response.status().as_u16() == 404 {
            return Ok(GroupState::Absent);
        }
        let value: Value = success(response).await?.json().await.map_err(io)?;
        Self::validate_ack(&value, id)?;
        let provisioning = value
            .pointer("/properties/provisioningState")
            .and_then(Value::as_str)
            .unwrap_or("");
        let state = value
            .pointer("/properties/instanceView/state")
            .and_then(Value::as_str)
            .unwrap_or("");
        Ok(
            if matches!(provisioning, "Failed" | "Canceled")
                || matches!(state, "Terminated" | "Succeeded" | "Failed" | "Stopped")
            {
                GroupState::Stopped
            } else {
                GroupState::Active
            },
        )
    }
    pub async fn start(&self, id: &str) -> Result<(), DataError> {
        let mut environment: Vec<Value> = self
            .config
            .environment
            .iter()
            .map(|(name, value)| json!({"name":name,"value":value}))
            .collect();
        environment.extend([
            json!({"name":"AZURE_CLIENT_ID","value":self.config.identity_client_id}),
            json!({"name":"LAKE_INGEST_MAX_WORKERS","value":self.config.max_workers.to_string()}),
            json!({"name":"LAKE_INGEST_TIMEOUT_SECONDS","value":self.config.timeout_seconds.to_string()}),
        ]);
        let body = json!({"location":self.config.location,"tags":{"fca-ingestion-job":id},"identity":{"type":"UserAssigned","userAssignedIdentities":{self.config.identity.clone():{}}},"properties":{
            "osType":"Linux","restartPolicy":"Never",
            "imageRegistryCredentials":[{"server":self.config.registry,"identity":self.config.identity}],
            "containers":[{"name":"ingest","properties":{"image":self.config.image,"command":["/usr/local/bin/lake-ingest-worker",id],"environmentVariables":environment,"resources":{"requests":{"cpu":self.config.cpu,"memoryInGB":self.config.memory_gb}}}}]
        }});
        let response = self
            .request(reqwest::Method::PUT, id)
            .await?
            .json(&body)
            .send()
            .await
            .map_err(io)?;
        let value = success(response).await?.json::<Value>().await.map_err(io)?;
        Self::validate_ack(&value, id)
    }
    pub async fn delete(&self, id: &str) -> Result<(), DataError> {
        let response = self
            .request(reqwest::Method::DELETE, id)
            .await?
            .send()
            .await
            .map_err(io)?;
        if response.status().as_u16() == 404 {
            return Ok(());
        }
        success(response).await.map(|_| ())
    }
    pub fn validate_ack(value: &Value, id: &str) -> Result<(), DataError> {
        if value.get("name").and_then(Value::as_str) != Some(Self::group_name(id).as_str())
            || value
                .pointer("/tags/fca-ingestion-job")
                .and_then(Value::as_str)
                != Some(id)
        {
            return Err(DataError::Conflict(
                "ACI acknowledgement does not match retained job ID".into(),
            ));
        }
        Ok(())
    }
}
