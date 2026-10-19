//! Conditional admission and lease-fenced updates over Azure Blob Storage.
use crate::{model::validate_id, ActiveJob, DurableJob};
use azure_core::auth::TokenCredential;
use lake_catalog::{JobRecord, JobRepository, JobState};
use lake_contracts::DataError;
use lake_storage::ReadableBlobStore;
use std::sync::Arc;

#[derive(Clone)]
pub struct JobStore {
    pub(crate) endpoint: String,
    pub(crate) credential: Arc<dyn TokenCredential>,
    pub(crate) http: reqwest::Client,
}
impl JobStore {
    pub fn new(endpoint: String, credential: Arc<dyn TokenCredential>) -> Result<Self, DataError> {
        Ok(Self {
            endpoint: endpoint.trim_end_matches('/').into(),
            credential,
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()
                .map_err(io)?,
        })
    }
    pub(crate) async fn request(
        &self,
        method: reqwest::Method,
        path: &str,
    ) -> Result<reqwest::RequestBuilder, DataError> {
        let token = self
            .credential
            .get_token(&["https://storage.azure.com/.default"])
            .await
            .map_err(io)?;
        Ok(self
            .http
            .request(
                method,
                if path.is_empty() {
                    self.endpoint.clone()
                } else {
                    format!("{}/{}", self.endpoint, path)
                },
            )
            .bearer_auth(token.token.secret())
            .header("x-ms-version", "2023-11-03")
            .header(
                "x-ms-date",
                httpdate::fmt_http_date(std::time::SystemTime::now()),
            ))
    }
    pub async fn admit(&self, job: DurableJob) -> Result<DurableJob, DataError> {
        self.admit_work(job).await
    }
    pub async fn admit_work(&self, job: DurableJob) -> Result<DurableJob, DataError> {
        let path = job.reference().path()?;
        let response = self
            .request(reqwest::Method::PUT, &path)
            .await?
            .header("If-None-Match", "*")
            .header("x-ms-blob-type", "BlockBlob")
            .header("x-ms-meta-active", "true")
            .header("x-ms-meta-dispatched", "false")
            .header(
                "x-ms-meta-created",
                job.record.created_at.micros().to_string(),
            )
            .json(&job)
            .send()
            .await
            .map_err(io)?;
        if already_exists(&response) {
            let existing = self.get_work(&job.reference()).await?;
            if existing.requests != job.requests
                || existing.partition != job.partition
                || (job.partition.is_some() && existing.execution != job.execution)
            {
                return Err(DataError::Conflict(
                    "job ID already has different ingestion input".into(),
                ));
            }
            return Ok(existing);
        }
        success(response).await?;
        Ok(job)
    }
    pub async fn get(&self, id: &str) -> Result<DurableJob, DataError> {
        self.get_work(&crate::WorkRef::root(id)).await
    }
    pub async fn get_work(&self, work: &crate::WorkRef) -> Result<DurableJob, DataError> {
        let response = self
            .request(reqwest::Method::GET, &work.path()?)
            .await?
            .send()
            .await
            .map_err(io)?;
        let response = success(response).await?;
        let job: DurableJob = response.json().await.map_err(io)?;
        if job.reference() != *work {
            return Err(DataError::Protocol(
                "stored ingestion job ID mismatch".into(),
            ));
        }
        Ok(job)
    }
    pub async fn own(&self, id: &str) -> Result<OwnedJob, DataError> {
        self.own_work(&crate::WorkRef::root(id)).await
    }
    pub async fn own_work(&self, work: &crate::WorkRef) -> Result<OwnedJob, DataError> {
        self.acquire(work.path()?).await
    }
    pub async fn dispatch_lock(&self) -> Result<OwnedJob, DataError> {
        let path = "ingestion/control/dispatcher.lock";
        let response = self
            .request(reqwest::Method::PUT, path)
            .await?
            .header("If-None-Match", "*")
            .header("x-ms-blob-type", "BlockBlob")
            .header(reqwest::header::CONTENT_LENGTH, 0)
            .body("")
            .send()
            .await
            .map_err(io)?;
        if !already_exists(&response) {
            success(response).await?;
        }
        self.acquire(path.into()).await
    }

    /// Metadata is committed atomically with the authoritative document, not maintained as a separate index.
    pub(crate) async fn active_jobs(&self) -> Result<Vec<ActiveJob>, DataError> {
        #[derive(serde::Deserialize)]
        struct Listing {
            #[serde(rename = "Blobs")]
            blobs: Blobs,
            #[serde(rename = "NextMarker", default)]
            marker: String,
        }
        #[derive(serde::Deserialize)]
        struct Blobs {
            #[serde(rename = "Blob", default)]
            items: Vec<Blob>,
        }
        #[derive(serde::Deserialize)]
        struct Blob {
            #[serde(rename = "Name")]
            name: String,
            #[serde(rename = "Metadata")]
            metadata: Metadata,
        }
        #[derive(serde::Deserialize)]
        struct Metadata {
            active: String,
            dispatched: String,
            created: String,
        }
        let mut marker = String::new();
        let mut jobs = Vec::new();
        loop {
            let response = self
                .request(reqwest::Method::GET, "")
                .await?
                .query(&[
                    ("restype", "container"),
                    ("comp", "list"),
                    ("prefix", "ingestion/jobs/"),
                    ("include", "metadata"),
                    ("marker", marker.as_str()),
                ])
                .send()
                .await
                .map_err(io)?;
            let payload = success(response).await?.text().await.map_err(io)?;
            let page: Listing = quick_xml::de::from_str(&payload).map_err(io)?;
            for blob in page.blobs.items {
                match blob.metadata.active.as_str() {
                    "false" => continue,
                    "true" => {}
                    _ => {
                        return Err(DataError::Protocol(
                            "invalid ingestion lifecycle metadata".into(),
                        ))
                    }
                }
                let id = blob
                    .name
                    .strip_prefix("ingestion/jobs/")
                    .and_then(|s| s.strip_suffix(".json"))
                    .ok_or_else(|| {
                        DataError::Protocol("invalid ingestion job object path".into())
                    })?;
                validate_id(id)?;
                let dispatched = match blob.metadata.dispatched.as_str() {
                    "true" => true,
                    "false" => false,
                    _ => {
                        return Err(DataError::Protocol(
                            "invalid ingestion dispatch metadata".into(),
                        ))
                    }
                };
                jobs.push(ActiveJob {
                    id: id.into(),
                    dispatched,
                    created: blob.metadata.created.parse().map_err(io)?,
                });
            }
            if page.marker.is_empty() {
                break;
            }
            if page.marker == marker {
                return Err(DataError::Protocol(
                    "Azure repeated the job listing cursor".into(),
                ));
            }
            marker = page.marker;
        }
        jobs.sort_by_key(|job| job.created);
        Ok(jobs)
    }
    async fn acquire(&self, path: String) -> Result<OwnedJob, DataError> {
        let response = self
            .request(reqwest::Method::PUT, &format!("{path}?comp=lease"))
            .await?
            .header("x-ms-lease-action", "acquire")
            .header("x-ms-lease-duration", "60")
            .header(reqwest::header::CONTENT_LENGTH, 0)
            .body("")
            .send()
            .await
            .map_err(io)?;
        let response = success(response).await?;
        let lease = response
            .headers()
            .get("x-ms-lease-id")
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| DataError::Io("lease acknowledgement is missing its ID".into()))?
            .into();
        Ok(OwnedJob {
            store: self.clone(),
            path,
            lease,
        })
    }
}

/// Azure conditional creation can report either an unmet condition or an existing block blob.
fn already_exists(response: &reqwest::Response) -> bool {
    response.status().as_u16() == 412
        || (response.status().as_u16() == 409
            && response
                .headers()
                .get("x-ms-error-code")
                .is_some_and(|v| v == "BlobAlreadyExists"))
}
pub struct OwnedJob {
    store: JobStore,
    path: String,
    lease: String,
}
impl OwnedJob {
    /// Renew independently of slow storage/control-plane calls and cancel work on lost ownership.
    pub async fn while_owned<T>(
        &self,
        work: impl std::future::Future<Output = Result<T, DataError>>,
    ) -> Result<T, DataError> {
        tokio::pin!(work);
        let mut heartbeat = tokio::time::interval_at(
            tokio::time::Instant::now() + std::time::Duration::from_secs(15),
            std::time::Duration::from_secs(15),
        );
        loop {
            tokio::select! {
                biased;
                _ = heartbeat.tick() => self.renew().await?,
                result = &mut work => return result,
            }
        }
    }

    pub async fn renew(&self) -> Result<(), DataError> {
        self.lease_action("renew").await
    }
    pub async fn release(&self) -> Result<(), DataError> {
        self.lease_action("release").await
    }
    async fn lease_action(&self, action: &str) -> Result<(), DataError> {
        success(
            self.store
                .request(reqwest::Method::PUT, &format!("{}?comp=lease", self.path))
                .await?
                .header("x-ms-lease-action", action)
                .header("x-ms-lease-id", &self.lease)
                .header(reqwest::header::CONTENT_LENGTH, 0)
                .body("")
                .send()
                .await
                .map_err(io)?,
        )
        .await
        .map(|_| ())
    }
    pub async fn save(&self, job: &DurableJob) -> Result<(), DataError> {
        if self.path != job.reference().path()? {
            return Err(DataError::Conflict("lease is for another job".into()));
        }
        self.store
            .get_work(&job.reference())
            .await?
            .validate_update(job)?;
        success(
            self.store
                .request(reqwest::Method::PUT, &self.path)
                .await?
                .header("x-ms-lease-id", &self.lease)
                .header("x-ms-blob-type", "BlockBlob")
                .header("x-ms-meta-active", (!job.execution_deleted).to_string())
                .header("x-ms-meta-dispatched", job.dispatch_started.to_string())
                .header(
                    "x-ms-meta-created",
                    job.record.created_at.micros().to_string(),
                )
                .json(job)
                .send()
                .await
                .map_err(io)?,
        )
        .await
        .map(|_| ())
    }
}
pub struct BlobJobRepository {
    store: Arc<dyn ReadableBlobStore>,
}
impl BlobJobRepository {
    pub fn new(store: Arc<dyn ReadableBlobStore>) -> Self {
        Self { store }
    }
}
impl JobRepository for BlobJobRepository {
    fn get(&self, id: &str) -> Result<Option<JobRecord>, DataError> {
        match self.store.read(&job_path(id)?) {
            Ok(bytes) => {
                let record = serde_json::from_slice::<DurableJob>(&bytes)
                    .map_err(io)?
                    .record;
                if record.job_id != id {
                    return Err(DataError::Protocol(
                        "stored ingestion job ID mismatch".into(),
                    ));
                }
                Ok(Some(record))
            }
            Err(DataError::NotFound(_)) => Ok(None),
            Err(e) => Err(e),
        }
    }
    fn upsert(&self, _: &JobRecord) -> Result<(), DataError> {
        Err(DataError::Conflict(
            "ingestion state requires the owning job lease".into(),
        ))
    }
    fn list(&self, state: Option<JobState>) -> Result<Vec<JobRecord>, DataError> {
        let mut records = Vec::new();
        for path in self.store.list_prefix("ingestion/jobs/")? {
            let record = serde_json::from_slice::<DurableJob>(&self.store.read(&path)?)
                .map_err(io)?
                .record;
            if state.is_none_or(|s| s == record.state) {
                records.push(record);
            }
        }
        records.sort_by(|a, b| a.job_id.cmp(&b.job_id));
        Ok(records)
    }
}
fn job_path(id: &str) -> Result<String, DataError> {
    validate_id(id)?;
    Ok(format!("ingestion/jobs/{id}.json"))
}
pub(crate) fn io(error: impl std::fmt::Display) -> DataError {
    DataError::Io(error.to_string())
}
pub(crate) async fn success(response: reqwest::Response) -> Result<reqwest::Response, DataError> {
    let status = response.status();
    if status.is_success() {
        return Ok(response);
    }
    let header_code = response
        .headers()
        .get("x-ms-error-code")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let payload = response.text().await.unwrap_or_default();
    let value = serde_json::from_str::<serde_json::Value>(&payload).ok();
    let code = value
        .as_ref()
        .and_then(|v| v.pointer("/error/code"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or(&header_code);
    let message = value
        .as_ref()
        .and_then(|v| v.pointer("/error/message"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    let detail = format!(
        "{status} {code}: {}",
        message.chars().take(1500).collect::<String>()
    );
    if matches!(status.as_u16(), 409 | 412) {
        Err(DataError::Conflict(format!("Azure conflict: {detail}")))
    } else if status.as_u16() == 404 {
        Err(DataError::NotFound(format!(
            "Azure resource not found: {detail}"
        )))
    } else {
        Err(DataError::Io(format!("Azure request failed: {detail}")))
    }
}

#[async_trait::async_trait]
impl crate::JobPersistence for JobStore {
    type Owner = OwnedJob;
    async fn admit(&self, job: DurableJob) -> Result<DurableJob, DataError> {
        self.admit(job).await
    }
    async fn get(&self, id: &str) -> Result<DurableJob, DataError> {
        self.get(id).await
    }
    async fn admit_work(&self, job: DurableJob) -> Result<DurableJob, DataError> {
        self.admit_work(job).await
    }
    async fn get_work(&self, work: &crate::WorkRef) -> Result<DurableJob, DataError> {
        self.get_work(work).await
    }
    async fn own_work(&self, work: &crate::WorkRef) -> Result<OwnedJob, DataError> {
        self.own_work(work).await
    }

    async fn own(&self, id: &str) -> Result<OwnedJob, DataError> {
        self.own(id).await
    }
    async fn dispatch_lock(&self) -> Result<OwnedJob, DataError> {
        self.dispatch_lock().await
    }
    async fn pending_work(&self) -> Result<Vec<crate::WorkRef>, DataError> {
        #[derive(serde::Deserialize)]
        struct Page {
            #[serde(rename = "Blobs")]
            blobs: Blobs,
            #[serde(rename = "NextMarker", default)]
            marker: String,
        }
        #[derive(serde::Deserialize)]
        struct Blobs {
            #[serde(rename = "Blob", default)]
            items: Vec<Blob>,
        }
        #[derive(serde::Deserialize)]
        struct Blob {
            #[serde(rename = "Name")]
            name: String,
            #[serde(rename = "Metadata")]
            metadata: Option<Metadata>,
        }
        #[derive(serde::Deserialize)]
        struct Metadata {
            active: String,
            dispatched: String,
        }
        let mut marker = String::new();
        let mut out = Vec::new();
        loop {
            let response = self
                .request(reqwest::Method::GET, "")
                .await?
                .query(&[
                    ("restype", "container"),
                    ("comp", "list"),
                    ("prefix", "ingestion/partitions/"),
                    ("include", "metadata"),
                    ("marker", marker.as_str()),
                ])
                .send()
                .await
                .map_err(io)?;
            let payload = success(response).await?.text().await.map_err(io)?;
            let page: Page = quick_xml::de::from_str(&payload).map_err(io)?;
            for blob in page.blobs.items {
                if !blob.name.ends_with(".json") {
                    continue;
                }
                let metadata = blob.metadata.ok_or_else(|| {
                    DataError::Protocol("ingestion chunk metadata is missing".into())
                })?;
                if metadata.active == "false" || metadata.dispatched == "true" {
                    continue;
                }
                if metadata.active != "true" || metadata.dispatched != "false" {
                    return Err(DataError::Protocol(
                        "invalid ingestion chunk metadata".into(),
                    ));
                }
                let address = blob
                    .name
                    .strip_prefix("ingestion/partitions/")
                    .and_then(|s| s.strip_suffix(".json"))
                    .and_then(|s| s.split_once('/'))
                    .ok_or_else(|| DataError::Protocol("invalid chunk path".into()))?;
                let reference = crate::WorkRef {
                    job_id: address.0.into(),
                    partition: Some(address.1.into()),
                };
                reference.path()?;
                out.push(reference);
            }
            if page.marker.is_empty() {
                break;
            }
            if marker == page.marker {
                return Err(DataError::Protocol(
                    "Azure repeated pending-work cursor".into(),
                ));
            }
            marker = page.marker;
        }
        Ok(out)
    }
    async fn active_jobs(&self) -> Result<Vec<ActiveJob>, DataError> {
        self.active_jobs().await
    }
}
#[async_trait::async_trait]
impl crate::JobOwnership for OwnedJob {
    async fn renew(&self) -> Result<(), DataError> {
        self.renew().await
    }
    async fn release(&self) -> Result<(), DataError> {
        self.release().await
    }
    async fn save(&self, job: &DurableJob) -> Result<(), DataError> {
        self.save(job).await
    }
}

#[async_trait::async_trait]
impl crate::RequestAuthority for JobStore {
    async fn reserve_requests(
        &self,
        scope: &str,
        limit: u32,
        count: u32,
    ) -> Result<crate::BudgetGrant, DataError> {
        self.reserve_requests(scope, limit, count).await
    }
}
