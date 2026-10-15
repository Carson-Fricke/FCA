//! Atomic current job documents with cross-process kernel ownership.
use crate::ownership::FileOwnership;
use lake_contracts::DataError;
use lake_ingest_jobs::{ActiveJob, DurableJob, JobOwnership, JobPersistence};
use lake_storage::{BlobStore, LocalFilesystemStore, ReadableBlobStore};
use std::{path::PathBuf, sync::Arc};

#[derive(Clone)]
pub struct LocalJobStore {
    root: PathBuf,
    objects: Arc<LocalFilesystemStore>,
}
impl LocalJobStore {
    pub fn new(root: PathBuf) -> Self {
        Self {
            objects: Arc::new(LocalFilesystemStore::new(&root)),
            root,
        }
    }
    fn key(id: &str) -> Result<String, DataError> {
        if id.is_empty()
            || id.len() > 128
            || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        {
            return Err(DataError::value("invalid ingestion job ID"));
        }
        Ok(format!("ingestion/jobs/{id}.json"))
    }
    pub fn is_owned(&self, id: &str) -> Result<bool, DataError> {
        match self.lock(id) {
            Ok(owner) => {
                owner.release()?;
                Ok(false)
            }
            Err(DataError::Conflict(_)) => Ok(true),
            Err(e) => Err(e),
        }
    }
    fn lock(&self, id: &str) -> Result<FileOwnership, DataError> {
        Self::key(id)?;
        FileOwnership::acquire(&self.root.join("ingestion/locks").join(format!("{id}.lock")))
    }
    pub fn read(&self, id: &str) -> Result<DurableJob, DataError> {
        let path = self.root.join(Self::key(id)?);
        if !path.exists() {
            return Err(DataError::NotFound("ingestion job not found".into()));
        }
        let job: DurableJob = serde_json::from_slice(&std::fs::read(path)?)
            .map_err(|e| DataError::Io(e.to_string()))?;
        if job.record.job_id != id {
            return Err(DataError::Protocol("stored job ID mismatch".into()));
        }
        Ok(job)
    }
}
pub struct LocalOwnedJob {
    store: LocalJobStore,
    id: Option<String>,
    ownership: FileOwnership,
}
#[async_trait::async_trait]
impl JobOwnership for LocalOwnedJob {
    async fn renew(&self) -> Result<(), DataError> {
        self.ownership.renew()
    }
    async fn release(&self) -> Result<(), DataError> {
        self.ownership.release()
    }
    async fn save(&self, job: &DurableJob) -> Result<(), DataError> {
        self.ownership.renew()?;
        self.store.read(&job.record.job_id)?.validate_update(job)?;
        if self.id.as_deref() != Some(&job.record.job_id) {
            return Err(DataError::Conflict("ownership is for another job".into()));
        }
        let bytes = serde_json::to_vec(job).map_err(|e| DataError::Io(e.to_string()))?;
        self.store
            .objects
            .replace(&LocalJobStore::key(&job.record.job_id)?, &bytes)
    }
}
#[async_trait::async_trait]
impl JobPersistence for LocalJobStore {
    type Owner = LocalOwnedJob;
    async fn admit(&self, job: DurableJob) -> Result<DurableJob, DataError> {
        let store = self.clone();
        tokio::task::spawn_blocking(move || {
            Self::key(&job.record.job_id)?;
            let guard = FileOwnership::acquire_wait(
                &store
                    .root
                    .join("ingestion/admission")
                    .join(format!("{}.lock", job.record.job_id)),
            )?;
            let result = match store.read(&job.record.job_id) {
                Ok(existing) if existing.requests == job.requests => Ok(existing),
                Ok(_) => Err(DataError::Conflict(
                    "job ID already has different ingestion input".into(),
                )),
                Err(DataError::NotFound(_)) => {
                    let owner = store.lock(&job.record.job_id)?;
                    store.objects.replace(
                        &Self::key(&job.record.job_id)?,
                        &serde_json::to_vec(&job).map_err(|e| DataError::Io(e.to_string()))?,
                    )?;
                    owner.release()?;
                    Ok(job)
                }
                Err(e) => Err(e),
            };
            guard.release()?;
            result
        })
        .await
        .map_err(|e| DataError::Io(e.to_string()))?
    }
    async fn get(&self, id: &str) -> Result<DurableJob, DataError> {
        self.read(id)
    }
    async fn own(&self, id: &str) -> Result<Self::Owner, DataError> {
        Ok(LocalOwnedJob {
            store: self.clone(),
            id: Some(id.into()),
            ownership: self.lock(id)?,
        })
    }
    async fn dispatch_lock(&self) -> Result<Self::Owner, DataError> {
        Ok(LocalOwnedJob {
            store: self.clone(),
            id: None,
            ownership: FileOwnership::acquire(&self.root.join("ingestion/dispatcher.lock"))?,
        })
    }
    async fn active_jobs(&self) -> Result<Vec<ActiveJob>, DataError> {
        let mut entries = Vec::new();
        for path in self.objects.list_prefix("ingestion/jobs")? {
            if !path.ends_with(".json") {
                continue;
            }
            let job: DurableJob = serde_json::from_slice(&self.objects.read(&path)?)
                .map_err(|e| DataError::Io(e.to_string()))?;
            if !job.group_deleted {
                entries.push(ActiveJob {
                    id: job.record.job_id,
                    dispatched: job.dispatch_started,
                    created: job.record.created_at.micros(),
                });
            }
        }
        entries.sort_by_key(|v| v.created);
        Ok(entries)
    }
}
