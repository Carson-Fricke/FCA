//! Atomic current job documents with cross-process kernel ownership.
use crate::ownership::FileOwnership;
use lake_contracts::DataError;
use lake_ingest_jobs::{
    ActiveJob, BudgetGrant, BudgetState, DurableJob, JobOwnership, JobPersistence, WorkRef,
};
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
    pub fn is_owned(&self, work: &WorkRef) -> Result<bool, DataError> {
        match self.lock(work) {
            Ok(owner) => {
                owner.release()?;
                Ok(false)
            }
            Err(DataError::Conflict(_)) => Ok(true),
            Err(e) => Err(e),
        }
    }
    fn lock(&self, work: &WorkRef) -> Result<FileOwnership, DataError> {
        FileOwnership::acquire(
            &self
                .root
                .join("ingestion/locks")
                .join(format!("{}.lock", work.worker_key()?)),
        )
    }
    pub fn read(&self, id: &str) -> Result<DurableJob, DataError> {
        self.read_work(&WorkRef::root(id))
    }
    pub fn read_work(&self, work: &WorkRef) -> Result<DurableJob, DataError> {
        let path = self.root.join(work.path()?);
        if !path.exists() {
            return Err(DataError::NotFound("ingestion job not found".into()));
        }
        let job: DurableJob = serde_json::from_slice(&std::fs::read(path)?)
            .map_err(|e| DataError::Io(e.to_string()))?;
        if job.reference() != *work {
            return Err(DataError::Protocol("stored job ID mismatch".into()));
        }
        Ok(job)
    }
}
pub struct LocalOwnedJob {
    store: LocalJobStore,
    id: Option<WorkRef>,
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
        self.store
            .read_work(&job.reference())?
            .validate_update(job)?;
        if self.id.as_ref() != Some(&job.reference()) {
            return Err(DataError::Conflict("ownership is for another job".into()));
        }
        let bytes = serde_json::to_vec(job).map_err(|e| DataError::Io(e.to_string()))?;
        self.store.objects.replace(&job.reference().path()?, &bytes)
    }
}
#[async_trait::async_trait]
impl JobPersistence for LocalJobStore {
    type Owner = LocalOwnedJob;
    async fn admit(&self, job: DurableJob) -> Result<DurableJob, DataError> {
        self.admit_work(job).await
    }
    async fn admit_work(&self, job: DurableJob) -> Result<DurableJob, DataError> {
        let store = self.clone();
        tokio::task::spawn_blocking(move || {
            let reference = job.reference();
            let guard = FileOwnership::acquire_wait(
                &store
                    .root
                    .join("ingestion/admission")
                    .join(format!("{}.lock", reference.worker_key()?)),
            )?;
            let result = match store.read_work(&reference) {
                Ok(existing)
                    if existing.requests == job.requests
                        && (job.partition.is_none() || existing.execution == job.execution) =>
                {
                    Ok(existing)
                }
                Ok(_) => Err(DataError::Conflict(
                    "job ID already has different ingestion input".into(),
                )),
                Err(DataError::NotFound(_)) => {
                    let owner = store.lock(&reference)?;
                    store.objects.replace(
                        &reference.path()?,
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
    async fn get_work(&self, work: &WorkRef) -> Result<DurableJob, DataError> {
        self.read_work(work)
    }
    async fn own(&self, id: &str) -> Result<Self::Owner, DataError> {
        self.own_work(&WorkRef::root(id)).await
    }
    async fn own_work(&self, work: &WorkRef) -> Result<Self::Owner, DataError> {
        Ok(LocalOwnedJob {
            store: self.clone(),
            id: Some(work.clone()),
            ownership: self.lock(work)?,
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
            if !job.execution_deleted {
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

// Shared OS uptime is comparable across local worker processes and cannot jump
// forward with wall-clock corrections. A reboot regression is fenced above.
#[cfg(windows)]
fn uptime_micros() -> Result<(i64, i64), DataError> {
    // SAFETY: GetTickCount64 takes no pointers and has no caller preconditions.
    let millis = unsafe { windows_sys::Win32::System::SystemInformation::GetTickCount64() };
    Ok((
        millis.saturating_mul(1000).min(i64::MAX as u64) as i64,
        20_000,
    ))
}
#[cfg(unix)]
fn uptime_micros() -> Result<(i64, i64), DataError> {
    let mut time = std::mem::MaybeUninit::<libc::timespec>::uninit();
    // SAFETY: clock_gettime initializes the supplied timespec on success.
    if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, time.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    // SAFETY: the successful call initialized both fields.
    let time = unsafe { time.assume_init() };
    if time.tv_sec < 0 || time.tv_nsec < 0 {
        return Err(DataError::value("invalid OS monotonic time"));
    }
    Ok((
        (time.tv_sec as i128 * 1_000_000 + time.tv_nsec as i128 / 1000).min(i64::MAX as i128)
            as i64,
        1,
    ))
}

#[async_trait::async_trait]
impl lake_ingest_jobs::RequestAuthority for LocalJobStore {
    async fn reserve_requests(
        &self,
        scope: &str,
        limit: u32,
        count: u32,
    ) -> Result<BudgetGrant, DataError> {
        let store = self.clone();
        let scope = scope.to_string();
        tokio::task::spawn_blocking(move || {
            if scope.len() != 64
                || !scope
                    .bytes()
                    .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
            {
                return Err(DataError::value("invalid request budget scope"));
            }
            let path = format!("ingestion/budgets/{scope}.json");
            let guard = FileOwnership::acquire_wait(
                &store
                    .root
                    .join("ingestion/locks")
                    .join(format!("budget-{scope}.lock")),
            )?;
            let mut state = match store.objects.read(&path) {
                Ok(bytes) => {
                    serde_json::from_slice::<BudgetState>(&bytes).map_err(DataError::io)?
                }
                Err(DataError::NotFound(_)) => BudgetState::new(limit)?,
                Err(error) => return Err(error),
            };
            if state.limit != limit {
                return Err(DataError::value("request budget configuration mismatch"));
            }
            let (now, precision) = uptime_micros()?;
            let restarted = now < state.observed_at;
            if restarted {
                state.hold_after_clock_restart(now, precision)?;
            }
            let grant = state.reserve_with_precision(limit, count, now, precision)?;
            if grant.count > 0 || restarted {
                store
                    .objects
                    .replace(&path, &serde_json::to_vec(&state).map_err(DataError::io)?)?;
            }
            guard.release()?;
            Ok(grant)
        })
        .await
        .map_err(DataError::io)?
    }
}
