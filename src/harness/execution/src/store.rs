//! One current durable job contract, shared by APIs and fenced finite workers.
use crate::jobs::*;
use crate::persistence::error;
use crate::persistence::Backend;
use crate::persistence::Result;
use chrono::DateTime;
use chrono::Utc;
use serde::Deserialize;
use serde::Serialize;
use std::collections::BTreeMap;
use std::sync::Arc;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Owner {
    pub token: String,
    pub expires: DateTime<Utc>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Document {
    pub record: JobRecord,
    #[serde(deserialize_with = "serde::Deserialize::deserialize")]
    pub owner: Option<Owner>,
    pub artifacts: BTreeMap<String, ArtifactInfo>,
    /// Optional outbox event, committed atomically with a terminal result.
    pub completion: Option<JobCompletion>,
}

/// Compact terminal evidence; no accepted source, credentials or delivery target.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JobCompletion {
    pub job_id: String,
    pub kind: JobKind,
    pub state: JobState,
    pub metrics: BTreeMap<String, String>,
    pub artifact_count: u64,
    pub error_message: Option<String>,
}

impl From<&JobRecord> for JobCompletion {
    fn from(record: &JobRecord) -> Self {
        Self {
            job_id: record.job_id.clone(),
            kind: record.kind,
            state: record.state,
            metrics: record.metrics.clone(),
            artifact_count: record.artifact_count,
            error_message: record.error_message.clone(),
        }
    }
}

#[derive(Clone)]
pub struct ExecutionStore {
    pub(crate) backend: Arc<dyn Backend>,
    owner: Option<(String, String)>,
    claimed_until: Option<DateTime<Utc>>,
}
fn path(id: &str) -> Result<String> {
    validate_id(id)?;
    Ok(format!("execution/jobs/{id}.json"))
}
fn validate_id(id: &str) -> Result<()> {
    if id.is_empty()
        || id.len() > 128
        || !id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
        || matches!(id, "." | "..")
    {
        return Err(error("invalid execution job_id"));
    }
    Ok(())
}
pub fn encode<T: Serialize>(value: &T) -> Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(error)
}
pub fn decode<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    serde_json::from_slice(bytes)
        .map_err(|e| error(format!("outdated or invalid execution document: {e}")))
}
impl ExecutionStore {
    pub fn new(backend: Arc<dyn Backend>) -> Self {
        Self {
            backend,
            owner: None,
            claimed_until: None,
        }
    }
    fn validate_publication(&self, record: &JobRecord, inventory: &[ArtifactInfo]) -> Result<()> {
        self.write(&record.job_id, |doc| {
            commit_result(&doc.record, record.clone(), inventory)?;
            Ok(())
        })
        .map(|_| ())
    }
    fn retain_published(
        &self,
        record: &JobRecord,
        info: &ArtifactInfo,
        inventory: &[ArtifactInfo],
    ) -> Result<()> {
        self.write(&record.job_id, |doc| {
            // Validate the intended transition and ownership before exposing evidence.
            commit_result(&doc.record, record.clone(), inventory)?;
            doc.artifacts.insert(info.name.clone(), info.clone());
            Ok(())
        })
        .map(|_| ())
    }
    fn finish_publication(
        &self,
        record: JobRecord,
        inventory: &[ArtifactInfo],
    ) -> Result<JobRecord> {
        self.write(&record.job_id, |doc| {
            let mut record = record.clone();
            record.updated_at = Utc::now();
            let mut artifacts = if record.state == JobState::Succeeded {
                BTreeMap::new()
            } else {
                doc.artifacts.clone()
            };
            artifacts.extend(inventory.iter().map(|i| (i.name.clone(), i.clone())));
            let inventory = artifacts.values().cloned().collect::<Vec<_>>();
            doc.record = commit_result(&doc.record, record, &inventory)?;
            doc.artifacts = artifacts;
            doc.completion = Some(JobCompletion::from(&doc.record));
            Ok(())
        })
    }
    pub fn document(&self, id: &str) -> Result<Option<Document>> {
        self.backend
            .read_metadata(&path(id)?)?
            .map(|bytes| {
                let doc: Document = decode(&bytes)?;
                if doc.record.job_id != id {
                    return Err(error("stored job_id mismatch"));
                }
                Ok(doc)
            })
            .transpose()
    }
    fn write(
        &self,
        id: &str,
        mut change: impl FnMut(&mut Document) -> Result<()>,
    ) -> Result<JobRecord> {
        let mut record = None;
        self.backend.update(&path(id)?, &mut |bytes| {
            record = None;
            let mut doc: Document = decode(&bytes)?;
            if let Some(owner) = &doc.owner {
                if self.owner.as_ref() != Some(&(id.into(), owner.token.clone()))
                    || owner.expires <= Utc::now()
                {
                    return Err(StoreError::OwnershipLost(id.into()));
                }
            }
            change(&mut doc)?;
            record = Some(doc.record.clone());
            encode(&doc)
        })?;
        record.ok_or_else(|| error("missing mutation result"))
    }
    pub fn claim(&self, id: &str) -> Result<Option<OwnedExecution>> {
        let token = uuid::Uuid::new_v4().to_string();
        let mut claimed = false;
        let mut claimed_until = None;
        self.backend.update(&path(id)?, &mut |bytes| {
            claimed = false;
            claimed_until = None;
            let mut doc: Document = decode(&bytes)?;
            if doc.record.state == JobState::Queued && doc.owner.is_none() {
                doc.owner = Some(Owner {
                    token: token.clone(),
                    expires: Utc::now() + chrono::Duration::seconds(60),
                });
                doc.record.state = JobState::Running;
                doc.record.updated_at = Utc::now();
            }
            if doc.record.state == JobState::Running
                && doc
                    .owner
                    .as_ref()
                    .is_some_and(|owner| owner.token == token && owner.expires > Utc::now())
            {
                claimed_until = doc.owner.as_ref().map(|o| o.expires);
                claimed = true;
            }
            encode(&doc)
        })?;
        Ok(claimed.then(|| OwnedExecution {
            id: id.into(),
            store: Self {
                backend: self.backend.clone(),
                owner: Some((id.into(), token)),
                claimed_until,
            },
        }))
    }

    pub fn claimed_until(&self) -> Option<DateTime<Utc>> {
        self.claimed_until
    }
    pub fn heartbeat(&self, id: &str) -> Result<Option<DateTime<Utc>>> {
        let mut expires = None;
        self.write(id, |doc| {
            expires = None;
            if doc.record.state.is_terminal() {
                return Ok(());
            }
            doc.owner
                .as_mut()
                .ok_or_else(|| StoreError::OwnershipLost(id.into()))?
                .expires = Utc::now() + chrono::Duration::seconds(60);
            expires = doc.owner.as_ref().map(|o| o.expires);
            Ok(())
        })
        .map(|_| expires)
    }
    pub fn recover_interrupted(&self) -> Result<()> {
        for id in self.ids()? {
            if !self.document(&id)?.is_some_and(|d| {
                !d.record.state.is_terminal() && d.owner.is_some_and(|o| o.expires <= Utc::now())
            }) {
                continue;
            }
            self.backend.update(&path(&id)?,&mut |bytes|{let mut doc:Document=decode(&bytes)?;
            if !doc.record.state.is_terminal() && doc.owner.as_ref().is_some_and(|owner|owner.expires<=Utc::now()) {
                let mut progress=harness_contracts::progress::ExecutionProgress::stage(harness_contracts::progress::ExecutionStage::Interrupted);progress.completed=false;apply_progress(&mut doc.record,progress,Utc::now())?;
                doc.record.state=JobState::Failed;doc.record.updated_at=Utc::now();doc.record.error_message=Some("execution worker disappeared or lost ownership; outcome is interrupted and will not be replayed".into());
                for progress in &mut doc.record.progress {if progress.finished_at.is_none(){progress.finished_at=Some(doc.record.updated_at);}}
                doc.completion=Some(JobCompletion::from(&doc.record));
            }
            encode(&doc)
        })?;
        }
        Ok(())
    }
    pub fn ids(&self) -> Result<Vec<String>> {
        self.backend
            .list("execution/jobs/")?
            .into_iter()
            .filter(|p| p.ends_with(".json"))
            .map(|p| {
                p.strip_prefix("execution/jobs/")
                    .and_then(|s| s.strip_suffix(".json"))
                    .map(str::to_string)
                    .ok_or_else(|| error("invalid execution object path"))
            })
            .collect()
    }
    pub fn queued(&self) -> Result<Vec<JobRecord>> {
        let mut jobs = Vec::new();
        for id in self.ids()? {
            let doc = self
                .document(&id)?
                .ok_or_else(|| error("execution disappeared"))?;
            if doc.record.state == JobState::Queued {
                jobs.push(doc.record);
            }
        }
        jobs.sort_by_key(|r| r.created_at);
        Ok(jobs)
    }

    /// Retry committed delivery events, never running-job status queries.
    pub fn pending_completions(&self) -> Result<Vec<JobCompletion>> {
        let mut events = Vec::new();
        for id in self.ids()? {
            if let Some(event) = self.document(&id)?.and_then(|doc| doc.completion) {
                events.push(event);
            }
        }
        Ok(events)
    }

    /// Clear only the immutable event acknowledged by its consumer. No owner or
    /// execution state is changed, and an interrupted ACK can safely be retried.
    pub fn acknowledge_completion(&self, event: &JobCompletion) -> Result<()> {
        self.backend.update(&path(&event.job_id)?, &mut |bytes| {
            let mut doc: Document = decode(&bytes)?;
            if doc
                .completion
                .as_ref()
                .is_some_and(|pending| pending != event)
            {
                return Err(error("completion acknowledgement mismatch"));
            }
            doc.completion = None;
            encode(&doc)
        })
    }
}
impl JobAdmission for ExecutionStore {
    fn admit(&self, record: JobRecord) -> Result<Admission> {
        let id = record.job_id.clone();
        let doc = Document {
            record: record.clone(),
            owner: None,
            artifacts: BTreeMap::new(),
            completion: None,
        };
        if self.backend.create(&path(&id)?, &encode(&doc)?)? {
            return Ok(Admission::Created(record));
        }
        let existing = self
            .document(&id)?
            .ok_or_else(|| error("admitted execution disappeared"))?
            .record;
        if existing.kind == record.kind
            && existing.input_digest == record.input_digest
            && existing.accepted_input == record.accepted_input
        {
            Ok(Admission::Joined(existing))
        } else {
            Err(StoreError::Conflict(id))
        }
    }
}
impl JobQueries for ExecutionStore {
    fn try_list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy: Option<&str>,
    ) -> Result<Vec<JobRecord>> {
        Ok(self
            .ids()?
            .into_iter()
            .map(|id| {
                self.try_get(&id)?
                    .ok_or_else(|| error("execution disappeared while listing"))
            })
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .filter(|r| {
                kind.is_none_or(|k| r.kind == k)
                    && state.is_none_or(|s| r.state == s)
                    && strategy.is_none_or(|s| r.strategy_id == s)
            })
            .collect::<Vec<_>>())
    }
    fn try_get(&self, id: &str) -> Result<Option<JobRecord>> {
        self.document(id).map(|doc| doc.map(|d| d.record))
    }
}
impl ArtifactReader for ExecutionStore {
    fn artifacts(&self, id: &str) -> Result<Option<Vec<ArtifactInfo>>> {
        self.document(id)
            .map(|doc| doc.map(|d| d.artifacts.into_values().collect()))
    }
    fn artifact(&self, id: &str, name: &str) -> Result<Option<Vec<u8>>> {
        validate_artifact_name(name)?;
        let Some(doc) = self.document(id)? else {
            return Ok(None);
        };
        let Some(info) = doc.artifacts.get(name) else {
            return Ok(None);
        };
        // Text/hex responses and the API's bounded binary transport cannot
        // materialize arbitrarily large result files in the control process.
        if info.size_bytes > 64 * 1024 * 1024 {
            return Err(error(
                "artifact exceeds inline read capacity; use its object download URI",
            ));
        }
        let bytes = self
            .backend
            .read(&info.uri)?
            .ok_or_else(|| error("published artifact is missing"))?;
        if bytes.len() as u64 != info.size_bytes || artifact_digest(&bytes) != info.sha256 {
            return Err(error("published artifact integrity mismatch"));
        }
        Ok(Some(bytes))
    }
    fn open_artifact(&self, id: &str, name: &str) -> Result<Option<Box<dyn std::io::Read + Send>>> {
        validate_artifact_name(name)?;
        let Some(doc) = self.document(id)? else {
            return Ok(None);
        };
        let Some(info) = doc.artifacts.get(name) else {
            return Ok(None);
        };
        Ok(Some(Box::new(VerifiedReader {
            inner: self.backend.open(&info.uri)?,
            hasher: sha2::Sha256::default(),
            size: 0,
            expected: info.clone(),
            done: false,
        })))
    }
}
impl ResultPublisher for ExecutionStore {
    fn mark_running(&self, id: &str, now: DateTime<Utc>) -> Result<JobRecord> {
        self.write(id, |doc| {
            if doc.record.state.is_terminal() {
                return Err(StoreError::Terminal(id.into()));
            }
            if doc.record.state == JobState::Queued {
                doc.record.state = JobState::Running;
                doc.record.updated_at = now.max(doc.record.updated_at);
            }
            Ok(())
        })
    }
    fn update_progress(
        &self,
        id: &str,
        snapshot: harness_contracts::progress::ExecutionProgress,
        now: DateTime<Utc>,
    ) -> Result<JobRecord> {
        self.write(id, |doc| {
            apply_progress(&mut doc.record, snapshot.clone(), now)
        })
    }
    fn put_result(
        &self,
        record: JobRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<JobRecord> {
        let id = record.job_id.clone();
        let inventory: Vec<_> = artifacts
            .iter()
            .map(|(name, bytes)| {
                let sha256 = artifact_digest(bytes);
                ArtifactInfo {
                    name: name.clone(),
                    uri: format!("execution/objects/{id}/{sha256}/{name}"),
                    sha256,
                    size_bytes: bytes.len() as u64,
                }
            })
            .collect();
        self.validate_publication(&record, &inventory)?;
        for info in &inventory {
            validate_artifact_name(&info.name)?;
            self.backend.create(&info.uri, &artifacts[&info.name])?;
            self.retain_published(&record, info, &inventory)?;
        }
        self.finish_publication(record, &inventory)
    }
    fn put_files(
        &self,
        record: JobRecord,
        files: BTreeMap<String, std::path::PathBuf>,
    ) -> Result<JobRecord> {
        use sha2::Digest;
        use std::io::Read;
        let id = record.job_id.clone();
        let mut progress = harness_contracts::progress::ExecutionProgress::stage(
            harness_contracts::progress::ExecutionStage::PublishingArtifacts,
        );
        let mut inventory = Vec::new();
        for (name, path) in &files {
            validate_artifact_name(name)?;
            let mut file = std::fs::File::open(path).map_err(error)?;
            let mut hash = sha2::Sha256::default();
            let mut bytes = 0u64;
            let mut buffer = vec![0; 64 * 1024];
            loop {
                let count = file.read(&mut buffer).map_err(error)?;
                if count == 0 {
                    break;
                }
                bytes += count as u64;
                hash.update(&buffer[..count]);
            }
            let sha256 = hex::encode(hash.finalize());
            inventory.push(ArtifactInfo {
                name: name.clone(),
                uri: format!("execution/objects/{id}/{sha256}/{name}"),
                sha256,
                size_bytes: bytes,
            });
        }
        self.validate_publication(&record, &inventory)?;
        self.update_progress(&id, progress.clone(), Utc::now())?;
        for info in &inventory {
            self.backend.create_file(&info.uri, &files[&info.name])?;
            self.retain_published(&record, info, &inventory)?;
            progress.bytes += info.size_bytes;
            self.update_progress(&id, progress.clone(), Utc::now())?;
        }
        progress.completed = true;
        self.update_progress(&id, progress, Utc::now())?;
        self.finish_publication(record, &inventory)
    }
}
impl JobRetention for ExecutionStore {
    fn remove(&self, id: &str) -> Result<bool> {
        let Some(doc) = self.document(id)? else {
            return Ok(false);
        };
        if !doc.record.state.is_terminal() {
            return Ok(false);
        }
        for info in doc.artifacts.values() {
            self.backend.delete(&info.uri)?;
        }
        self.backend.delete(&path(id)?)?;
        Ok(true)
    }
}

struct VerifiedReader {
    inner: Box<dyn std::io::Read + Send>,
    hasher: sha2::Sha256,
    size: u64,
    expected: ArtifactInfo,
    done: bool,
}
impl std::io::Read for VerifiedReader {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        use sha2::Digest;
        if self.done {
            return Ok(0);
        }
        if buffer.is_empty() {
            return Ok(0);
        }
        let count = self.inner.read(buffer)?;
        self.size += count as u64;
        self.hasher.update(&buffer[..count]);
        if self.size > self.expected.size_bytes {
            return Err(std::io::Error::other("artifact exceeds published size"));
        }
        if count == 0 {
            self.done = true;
            if self.size != self.expected.size_bytes
                || hex::encode(self.hasher.clone().finalize()) != self.expected.sha256
            {
                return Err(std::io::Error::other("artifact integrity mismatch"));
            }
        }
        Ok(count)
    }
}

/// Opaque fenced authority for exactly one execution. Only successful claim constructs it.
/// It grants neither admission, deletion nor access to unrelated jobs/artifacts.
#[derive(Clone)]
pub struct OwnedExecution {
    id: String,
    store: ExecutionStore,
}
impl OwnedExecution {
    pub fn job_id(&self) -> &str {
        &self.id
    }
    fn require(&self, id: &str) -> Result<()> {
        if id == self.id {
            Ok(())
        } else {
            Err(StoreError::OwnershipLost(id.into()))
        }
    }
    pub fn claimed_until(&self) -> Option<DateTime<Utc>> {
        self.store.claimed_until()
    }
    pub fn heartbeat(&self, id: &str) -> Result<Option<DateTime<Utc>>> {
        self.require(id)?;
        self.store.heartbeat(id)
    }
}
impl JobQueries for OwnedExecution {
    fn try_get(&self, id: &str) -> Result<Option<JobRecord>> {
        self.require(id)?;
        self.store.try_get(id)
    }
    fn try_list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy: Option<&str>,
    ) -> Result<Vec<JobRecord>> {
        Ok(self
            .store
            .try_get(&self.id)?
            .into_iter()
            .filter(|record| {
                kind.is_none_or(|kind| record.kind == kind)
                    && state.is_none_or(|state| record.state == state)
                    && strategy.is_none_or(|strategy| record.strategy_id == strategy)
            })
            .collect())
    }
}
impl ResultPublisher for OwnedExecution {
    fn mark_running(&self, id: &str, now: DateTime<Utc>) -> Result<JobRecord> {
        self.require(id)?;
        self.store.mark_running(id, now)
    }
    fn update_progress(
        &self,
        id: &str,
        progress: harness_contracts::progress::ExecutionProgress,
        now: DateTime<Utc>,
    ) -> Result<JobRecord> {
        self.require(id)?;
        self.store.update_progress(id, progress, now)
    }
    fn put_result(
        &self,
        record: JobRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<JobRecord> {
        self.require(&record.job_id)?;
        self.store.put_result(record, artifacts)
    }
    fn put_files(
        &self,
        record: JobRecord,
        files: BTreeMap<String, std::path::PathBuf>,
    ) -> Result<JobRecord> {
        self.require(&record.job_id)?;
        self.store.put_files(record, files)
    }
}
