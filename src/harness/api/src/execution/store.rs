//! One current durable job contract, shared by APIs and fenced finite workers.
use super::backend::{error, Backend, Result};
use chrono::{DateTime, Utc};
use mcp::store::*;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, sync::Arc};

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
}

#[derive(Clone)]
pub struct ExecutionStore {
    pub backend: Arc<dyn Backend>,
    pub owner: Option<(String, String)>,
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
    pub fn claim(&self, id: &str) -> Result<Option<Self>> {
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
        Ok(claimed.then(|| Self {
            backend: self.backend.clone(),
            owner: Some((id.into(), token)),
            claimed_until,
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
}
impl JobStore for ExecutionStore {
    fn get(&self, id: &str) -> Option<JobRecord> {
        match self.document(id) {
            Ok(doc) => doc.map(|d| d.record),
            Err(e) => {
                eprintln!("execution metadata read failed: {e}");
                None
            }
        }
    }
    fn admit(&self, record: JobRecord) -> Result<Admission> {
        let id = record.job_id.clone();
        let doc = Document {
            record: record.clone(),
            owner: None,
            artifacts: BTreeMap::new(),
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
        for info in &inventory {
            validate_artifact_name(&info.name)?;
            self.backend.create(&info.uri, &artifacts[&info.name])?;
        }
        self.write(&id, |doc| {
            let mut record = record.clone();
            record.updated_at = Utc::now();
            doc.record = commit_result(&doc.record, record, &inventory)?;
            doc.artifacts = inventory
                .iter()
                .map(|info| (info.name.clone(), info.clone()))
                .collect();
            Ok(())
        })
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
        self.update_progress(&id, progress.clone(), Utc::now())?;
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
        for info in &inventory {
            self.backend.create_file(&info.uri, &files[&info.name])?;
            progress.bytes += info.size_bytes;
            self.update_progress(&id, progress.clone(), Utc::now())?;
        }
        progress.completed = true;
        self.update_progress(&id, progress, Utc::now())?;
        self.write(&id, |doc| {
            let mut record = record.clone();
            record.updated_at = Utc::now();
            doc.record = commit_result(&doc.record, record, &inventory)?;
            doc.artifacts = inventory
                .iter()
                .map(|info| (info.name.clone(), info.clone()))
                .collect();
            Ok(())
        })
    }
    fn list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy: Option<&str>,
    ) -> Vec<JobRecord> {
        self.try_list(kind, state, strategy)
            .unwrap_or_else(|error| {
                eprintln!("execution metadata list failed: {error}");
                Vec::new()
            })
    }
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
    fn fetch(&self, id: &str) -> Option<StoredJob> {
        let record = self.try_get(id).ok()??;
        let mut artifacts = BTreeMap::new();
        for info in self.artifacts(id).ok()?? {
            artifacts.insert(info.name.clone(), self.artifact(id, &info.name).ok()??);
        }
        Some(StoredJob { record, artifacts })
    }
    fn delete(&self, id: &str) -> bool {
        let Ok(Some(doc)) = self.document(id) else {
            return false;
        };
        if !doc.record.state.is_terminal() {
            return false;
        }
        for info in doc.artifacts.values() {
            if self.backend.delete(&info.uri).is_err() {
                return false;
            }
        }
        self.backend.delete(&path(id).unwrap()).is_ok()
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
