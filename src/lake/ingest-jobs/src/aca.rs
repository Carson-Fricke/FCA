//! Finite ACA Job executions. A possibly accepted start is never automatically replayed.
use crate::store::{io, success};
use crate::WorkRef;
use azure_core::auth::TokenCredential;
use lake_contracts::DataError;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::BTreeMap, sync::Arc};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AcaConfig {
    pub subscription: String,
    pub resource_group: String,
    pub job_name: String,
    pub image: String,
    pub worker_client_id: String,
    pub cpu: f64,
    pub memory_gib: f64,
    pub max_workers: usize,
    pub timeout_seconds: u64,
    pub partition_days: u16,
    pub environment: BTreeMap<String, String>,
}
impl AcaConfig {
    pub fn from_environment(env: &BTreeMap<String, String>) -> Result<Self, DataError> {
        let required = |key: &str| {
            env.get(key)
                .filter(|s| !s.trim().is_empty())
                .cloned()
                .ok_or_else(|| DataError::value(format!("{key} is required")))
        };
        let number = |key: &str, fallback: f64| {
            env.get(key)
                .map(|s| s.parse().map_err(io))
                .unwrap_or(Ok(fallback))
        };
        let cpu = number("LAKE_INGEST_CPU", 4.0)?;
        let memory_gib = number("LAKE_INGEST_MEMORY_GIB", 8.0)?;
        let workers = number("LAKE_INGEST_MAX_WORKERS", 8.0)?;
        let timeout = number("LAKE_INGEST_TIMEOUT_SECONDS", 86400.0)?;
        let days = number("LAKE_INGEST_PARTITION_DAYS", 31.0)?;
        if !cpu.is_finite()
            || cpu <= 0.0
            || !memory_gib.is_finite()
            || memory_gib <= 0.0
            || workers.fract() != 0.0
            || !(1.0..=64.0).contains(&workers)
            || timeout.fract() != 0.0
            || !(60.0..=604800.0).contains(&timeout)
            || days.fract() != 0.0
            || !(1.0..=366.0).contains(&days)
        {
            return Err(DataError::value(
                "invalid ingestion resources or partition size",
            ));
        }
        let image = required("LAKE_INGEST_IMAGE")?;
        if !image.rsplit_once("@sha256:").is_some_and(|(_, digest)| {
            digest.len() == 64
                && digest
                    .bytes()
                    .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        }) {
            return Err(DataError::value("ingestion image must be digest pinned"));
        }
        let environment = env
            .iter()
            .filter(|(key, _)| {
                matches!(
                    key.as_str(),
                    "LAKE_PROVIDER__REQUEST__MAX_CONCURRENCY"
                        | "LAKE_PROVIDER__REQUEST__REQUESTS_PER_MINUTE"
                        | "LAKE_PROVIDER__REQUEST__TIMEOUT_SECONDS"
                        | "LAKE_PROVIDER__REQUEST__RETRY__MAX_ATTEMPTS"
                        | "LAKE_PROVIDER__REQUEST__RETRY__HONOR_RETRY_AFTER"
                        | "LAKE_INGEST_SESSION_CONCURRENCY"
                        | "LAKE_INGEST_CPU_SLOTS"
                        | "LAKE_INGEST_RESPONSE_MEMORY_MIB"
                        | "LAKE_INGEST_MAX_RESPONSE_MIB"
                        | "LAKE_INGEST_MEMORY_MIB"
                        | "LAKE_INGEST_SESSION_MIB"
                        | "LAKE_INGEST_SCRATCH_MIB"
                        | "LAKE_INGEST_PUBLICATION_CONCURRENCY"
                        | "LAKE_STORAGE_ACCOUNT_NAME"
                        | "LAKE_STORAGE_CONTAINER"
                        | "LAKE_COVERAGE_TABLE"
                        | "LAKE_PROVIDER_KEY_VAULT_URL"
                )
            })
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        Ok(Self {
            subscription: required("AZURE_SUBSCRIPTION_ID")?,
            resource_group: required("LAKE_INGEST_RESOURCE_GROUP")?,
            job_name: required("LAKE_INGEST_JOB_NAME")?,
            image,
            worker_client_id: required("LAKE_INGEST_WORKER_CLIENT_ID")?,
            cpu,
            memory_gib,
            max_workers: workers as usize,
            timeout_seconds: timeout as u64,
            partition_days: days as u16,
            environment,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecutionState {
    Absent,
    Active,
    Stopped,
}

type ExecutionSnapshot = Arc<std::sync::Mutex<Option<(String, Arc<Vec<Value>>)>>>;

#[derive(Clone)]
pub struct AcaRunner {
    pub config: AcaConfig,
    credential: Arc<dyn TokenCredential>,
    http: reqwest::Client,
    endpoint: String,
    executions: ExecutionSnapshot,
}
impl AcaRunner {
    pub fn new(config: AcaConfig, credential: Arc<dyn TokenCredential>) -> Result<Self, DataError> {
        Ok(Self {
            config,
            credential,
            http: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(std::time::Duration::from_secs(30))
                .build()
                .map_err(io)?,
            endpoint: "https://management.azure.com".into(),
            executions: Arc::default(),
        })
    }
    pub fn for_execution(&self, config: AcaConfig) -> Self {
        Self {
            config,
            ..self.clone()
        }
    }
    pub fn begin_reconcile(&self) {
        *self
            .executions
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
    }
    fn resource(&self) -> String {
        format!("{}{}", self.endpoint, self.resource_id())
    }
    fn resource_id(&self) -> String {
        format!(
            "/subscriptions/{}/resourceGroups/{}/providers/Microsoft.App/jobs/{}",
            self.config.subscription, self.config.resource_group, self.config.job_name
        )
    }
    async fn request(
        &self,
        method: reqwest::Method,
        url: &str,
    ) -> Result<reqwest::RequestBuilder, DataError> {
        let token = self
            .credential
            .get_token(&["https://management.azure.com/.default"])
            .await
            .map_err(io)?;
        Ok(self
            .http
            .request(method, url)
            .bearer_auth(token.token.secret()))
    }
    fn correlation(value: &Value, work: &WorkRef) -> bool {
        value
            .pointer("/properties/template/containers")
            .and_then(Value::as_array)
            .is_some_and(|containers| {
                containers
                    .iter()
                    .filter(|container| {
                        container.get("name").and_then(Value::as_str) == Some("ingest")
                    })
                    .any(|container| {
                        let env = container.get("env").and_then(Value::as_array);
                        let variable = |name: &str| {
                            env.and_then(|values| {
                                values
                                    .iter()
                                    .find(|v| v.get("name").and_then(Value::as_str) == Some(name))
                            })
                            .and_then(|v| v.get("value"))
                            .and_then(Value::as_str)
                        };
                        variable("LAKE_INGEST_JOB_ID") == Some(work.job_id.as_str())
                            && variable("LAKE_INGEST_PARTITION") == work.partition.as_deref()
                    })
            })
    }
    fn validate_base(&self, value: &Value) -> Result<String, DataError> {
        let name = value
            .get("name")
            .and_then(Value::as_str)
            .filter(|name| {
                !name.is_empty()
                    && name.len() <= 256
                    && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
            })
            .ok_or_else(|| {
                DataError::Protocol("ACA execution acknowledgement has no valid name".into())
            })?;
        let expected_id = format!("{}/executions/{name}", self.resource_id());
        if !name.starts_with(&format!("{}-", self.config.job_name))
            || !value
                .get("id")
                .and_then(Value::as_str)
                .is_some_and(|id| id.eq_ignore_ascii_case(&expected_id))
        {
            return Err(DataError::Protocol(
                "ACA execution acknowledgement name or resource ID does not match the expected Job"
                    .into(),
            ));
        }
        Ok(name.into())
    }
    fn validate_ack(&self, value: &Value, work: &WorkRef) -> Result<String, DataError> {
        let name = self.validate_base(value)?;
        if !Self::correlation(value, work) {
            return Err(DataError::Protocol(
                "ACA execution template does not match retained work".into(),
            ));
        }
        let image = value
            .pointer("/properties/template/containers")
            .and_then(Value::as_array)
            .and_then(|values| {
                values
                    .iter()
                    .find(|v| v.get("name").and_then(Value::as_str) == Some("ingest"))
            })
            .and_then(|v| v.get("image"))
            .and_then(Value::as_str);
        if image != Some(self.config.image.as_str()) {
            return Err(DataError::Protocol("ACA execution image mismatch".into()));
        }
        Ok(name)
    }
    fn operation_location(&self, location: &str) -> Result<String, DataError> {
        let url = reqwest::Url::parse(location).map_err(io)?;
        let endpoint = reqwest::Url::parse(&self.endpoint).map_err(io)?;
        if url.origin() != endpoint.origin()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
            || !url.path().to_ascii_lowercase().starts_with(&format!(
                "/subscriptions/{}/",
                self.config.subscription.to_ascii_lowercase()
            ))
        {
            return Err(DataError::Protocol(
                "ACA start returned an unsafe operation Location".into(),
            ));
        }
        Ok(url.into())
    }
    /// Only GET polling follows an accepted start. Failure leaves its durable
    /// intent unresolved for attributed execution-list reconciliation.
    async fn start_result(&self, mut response: reqwest::Response) -> Result<Value, DataError> {
        let mut location = None;
        for _ in 0..32 {
            match response.status() {
                reqwest::StatusCode::OK => return response.json().await.map_err(io),
                reqwest::StatusCode::ACCEPTED => {
                    if let Some(header) = response.headers().get(reqwest::header::LOCATION) {
                        location = Some(self.operation_location(header.to_str().map_err(io)?)?);
                    }
                    let location = location.as_ref().ok_or_else(|| {
                        DataError::Protocol("ACA accepted start has no operation Location".into())
                    })?;
                    let seconds = response
                        .headers()
                        .get(reqwest::header::RETRY_AFTER)
                        .map(|header| header.to_str().map_err(io)?.parse::<u64>().map_err(io))
                        .transpose()?
                        .unwrap_or(1);
                    let delay = std::time::Duration::from_secs(seconds).clamp(
                        std::time::Duration::from_millis(1),
                        std::time::Duration::from_secs(30),
                    );
                    tokio::time::sleep(delay).await;
                    response = self
                        .request(reqwest::Method::GET, location)
                        .await?
                        .send()
                        .await
                        .map_err(io)?;
                }
                _ => {
                    success(response).await?;
                    return Err(DataError::Protocol(
                        "unexpected ACA start operation response".into(),
                    ));
                }
            }
        }
        Err(DataError::Protocol(
            "ACA start operation did not resolve within its polling bound".into(),
        ))
    }
    async fn verify_started(
        &self,
        response: reqwest::Response,
        work: &WorkRef,
    ) -> Result<(), DataError> {
        let value = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            self.start_result(response),
        )
        .await
        .map_err(|_| DataError::Protocol("ACA start operation resolution timed out".into()))??;
        let name = self.validate_base(&value)?;
        let response = self
            .request(
                reqwest::Method::GET,
                &format!(
                    "{}/executions/{name}?api-version=2025-07-01",
                    self.resource()
                ),
            )
            .await?
            .send()
            .await
            .map_err(io)?;
        let execution = success(response).await?.json::<Value>().await.map_err(io)?;
        if self.validate_ack(&execution, work)? != name {
            return Err(DataError::Protocol(
                "ACA execution GET acknowledgement changed its name".into(),
            ));
        }
        Ok(())
    }
    async fn execution_snapshot(&self) -> Result<Arc<Vec<Value>>, DataError> {
        let resource = self.resource();
        if let Some((key, values)) = self
            .executions
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
        {
            if *key == resource {
                return Ok(values.clone());
            }
        }
        let mut url = format!("{}/executions?api-version=2025-07-01", self.resource());
        let mut snapshot = Vec::new();
        let mut visited = std::collections::BTreeSet::new();
        loop {
            if !visited.insert(url.clone()) {
                return Err(DataError::Protocol(
                    "ACA repeated an execution listing cursor".into(),
                ));
            }
            let response = self
                .request(reqwest::Method::GET, &url)
                .await?
                .send()
                .await
                .map_err(io)?;
            let value: Value = success(response).await?.json().await.map_err(io)?;
            let executions = value
                .get("value")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    DataError::Protocol("ACA execution listing has no value array".into())
                })?;
            snapshot.extend(executions.iter().cloned());
            match value
                .get("nextLink")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                None => {
                    let snapshot = Arc::new(snapshot);
                    *self
                        .executions
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner) =
                        Some((resource, snapshot.clone()));
                    return Ok(snapshot);
                }
                Some(next) if next.starts_with(&format!("{}/executions?", self.resource())) => {
                    url = next.into()
                }
                Some(_) => {
                    return Err(DataError::Protocol(
                        "invalid ACA execution listing cursor".into(),
                    ))
                }
            }
        }
    }
    async fn execution(
        &self,
        work: &WorkRef,
    ) -> Result<Option<(String, ExecutionState)>, DataError> {
        work.path()?;
        let snapshot = self.execution_snapshot().await?;
        let mut found = None;
        for execution in snapshot
            .iter()
            .filter(|value| Self::correlation(value, work))
        {
            let name = self.validate_ack(execution, work)?;
            if found.is_some() {
                return Err(DataError::Conflict(
                    "more than one ACA execution owns the same partition".into(),
                ));
            }
            let status = execution
                .pointer("/properties/status")
                .and_then(Value::as_str)
                .ok_or_else(|| DataError::Protocol("ACA execution status is missing".into()))?;
            let state = if matches!(status, "Succeeded" | "Failed" | "Stopped") {
                ExecutionState::Stopped
            } else {
                ExecutionState::Active
            };
            found = Some((name, state));
        }
        Ok(found)
    }
    pub async fn inspect(&self, work: &WorkRef) -> Result<ExecutionState, DataError> {
        Ok(self
            .execution(work)
            .await?
            .map_or(ExecutionState::Absent, |(_, state)| state))
    }
    pub async fn start(&self, work: &WorkRef) -> Result<(), DataError> {
        let partition = work
            .partition
            .as_ref()
            .ok_or_else(|| DataError::value("only an admitted partition can start"))?;
        work.path()?;
        let mut env: Vec<Value> = self
            .config
            .environment
            .iter()
            .filter(|(name, _)| {
                !matches!(
                    name.as_str(),
                    "AZURE_CLIENT_ID"
                        | "LAKE_INGEST_JOB_ID"
                        | "LAKE_INGEST_PARTITION"
                        | "LAKE_INGEST_TIMEOUT_SECONDS"
                )
            })
            .map(|(name, value)| json!({"name": name, "value": value}))
            .collect();
        env.extend([
            json!({"name":"AZURE_CLIENT_ID","value":self.config.worker_client_id}),
            json!({"name":"LAKE_INGEST_JOB_ID","value":work.job_id}),
            json!({"name":"LAKE_INGEST_PARTITION","value":partition}),
            json!({"name":"LAKE_INGEST_TIMEOUT_SECONDS","value":self.config.timeout_seconds.to_string()}),
        ]);
        let template = json!({"containers":[{"name":"ingest","image":self.config.image,
            "command":["/usr/local/bin/lake-ingest-worker"],"args":[work.job_id,partition], "env":env,
            "resources":{"cpu":self.config.cpu,"memory":format!("{}Gi",self.config.memory_gib)}}]});
        let response = self
            .request(
                reqwest::Method::POST,
                &format!("{}/start?api-version=2025-07-01", self.resource()),
            )
            .await?
            .json(&template)
            .send()
            .await;
        self.begin_reconcile();
        let result = match response {
            Ok(response) => self.verify_started(response, work).await,
            Err(error) => Err(io(error)),
        };
        result.map_err(|error| {
            DataError::Protocol(format!(
                "ACA start outcome is unknown; reconcile retained work without replay: {error}"
            ))
        })
    }
    pub async fn delete(&self, work: &WorkRef) -> Result<(), DataError> {
        if let Some((name, ExecutionState::Active)) = self.execution(work).await? {
            success(
                self.request(
                    reqwest::Method::POST,
                    &format!(
                        "{}/executions/{name}/stop?api-version=2025-07-01",
                        self.resource()
                    ),
                )
                .await?
                .send()
                .await
                .map_err(io)?,
            )
            .await?;
            self.begin_reconcile();
        }
        Ok(())
    }
}

#[cfg(test)]
#[path = "../tests/unit/azure_http.rs"]
mod tests;
