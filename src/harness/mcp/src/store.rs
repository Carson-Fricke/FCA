//! Shared job persistence for the harness HTTP and MCP surfaces.
//!
//! This module is the single durable-store definition used by both the axum
//! HTTP boundary (`api::http::store`) and the MCP server (`crate::tools`). One
//! authoritative job record carries lifecycle and result metadata for every
//! kind; artifact payloads are stored independently below the `jobs` object
//! prefix. Job result digests are domain-separated SHA-256 over canonical JSON,
//! and the in-memory adapter keeps tests free of filesystem access while the
//! filesystem adapter survives process restarts.

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Blob prefix holding one job's artifacts.
pub const ARTIFACT_PREFIX: &str = "jobs";
const JOB_DIGEST_DOMAIN: &[u8] = b"fca:harness-results:job:v1\0";
const JOB_DIGEST_VERSION: &str = "harness.results.job.v1";

/// The kind of execution one job represents.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum JobKind {
    /// One deterministic backtest.
    Backtest,
    /// One deterministic study replay.
    Study,
    /// One cross-sectional screening run.
    Screen,
}

impl JobKind {
    /// Parse the wire spelling of a job kind.
    ///
    /// # Errors
    ///
    /// Returns [`InvalidStatus`] for any unrecognized value.
    pub fn parse(value: &str) -> Result<Self, InvalidStatus> {
        match value {
            "backtest" => Ok(Self::Backtest),
            "study" => Ok(Self::Study),
            "screen" => Ok(Self::Screen),
            other => Err(InvalidStatus(other.to_string())),
        }
    }

    /// The wire spelling of this kind.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Backtest => "backtest",
            Self::Study => "study",
            Self::Screen => "screen",
        }
    }
}

/// Lifecycle states for one stored job.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum JobState {
    /// Accepted but not started.
    Queued,
    /// Currently executing.
    Running,
    /// Finished successfully.
    Succeeded,
    /// Finished with an error.
    Failed,
    /// Cancelled before completion.
    Cancelled,
}

impl JobState {
    /// Parse the wire spelling of a job state.
    ///
    /// # Errors
    ///
    /// Returns [`InvalidStatus`] for any unrecognized value.
    pub fn parse(value: &str) -> Result<Self, InvalidStatus> {
        match value {
            "queued" => Ok(Self::Queued),
            "running" => Ok(Self::Running),
            "succeeded" => Ok(Self::Succeeded),
            "failed" => Ok(Self::Failed),
            "cancelled" => Ok(Self::Cancelled),
            other => Err(InvalidStatus(other.to_string())),
        }
    }

    /// The wire spelling of this state.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }

    /// Whether this state is terminal.
    #[must_use]
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Succeeded | Self::Failed | Self::Cancelled)
    }
}

/// A parsed lifecycle value that was not one of the defined states.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InvalidStatus(pub String);

impl fmt::Display for InvalidStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "unknown status value: {}", self.0)
    }
}

impl std::error::Error for InvalidStatus {}

/// A durable stage/dataset observation; updates retain the first timestamp.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JobProgress {
    #[serde(flatten)]
    pub snapshot: harness_contracts::progress::ExecutionProgress,
    pub started_at: DateTime<Utc>,
    pub finished_at: Option<DateTime<Utc>>,
    pub updated_at: DateTime<Utc>,
}

/// The authoritative record for one harness execution.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JobRecord {
    /// Job identifier (a single safe path segment).
    pub job_id: String,
    /// Execution kind.
    pub kind: JobKind,
    /// Current lifecycle state.
    pub state: JobState,
    /// Creation timestamp.
    pub created_at: DateTime<Utc>,
    /// Last update timestamp.
    pub updated_at: DateTime<Utc>,
    /// Failure message, when the job failed.
    pub error_message: Option<String>,
    /// Latest counters for each observed stage/dataset, retained on failure.
    #[serde(default)]
    pub progress: Vec<JobProgress>,
    /// Identifier of the strategy (or comma-joined instance identifiers).
    pub strategy_id: String,
    /// First session date, when known.
    pub data_start: Option<NaiveDate>,
    /// Last session date, when known.
    pub data_end: Option<NaiveDate>,
    /// Domain-separated digest over the job's artifact digests.
    pub run_digest: Option<String>,
    /// Hash of the submitted configuration that produced the job.
    pub configuration_digest: Option<String>,
    /// Number of stored artifacts.
    pub artifact_count: u64,
    /// Total bytes stored under the job object prefix.
    pub total_bytes: u64,
    /// Headline metrics, kept string-exact.
    pub metrics: BTreeMap<String, String>,
    /// Accepted-input fingerprint used for admission conflict detection.
    pub input_digest: String,
    /// Immutable submitted document retained at admission for provenance validation.
    pub accepted_input: serde_json::Value,
}

impl JobRecord {
    /// Construct a queued job record with empty derived fields.
    #[must_use]
    pub fn new(
        job_id: impl Into<String>,
        kind: JobKind,
        input_digest: impl Into<String>,
        now: DateTime<Utc>,
    ) -> Self {
        Self {
            job_id: job_id.into(),
            kind,
            state: JobState::Queued,
            created_at: now,
            updated_at: now,
            error_message: None,
            progress: Vec::new(),
            strategy_id: String::new(),
            data_start: None,
            data_end: None,
            run_digest: None,
            configuration_digest: None,
            artifact_count: 0,
            total_bytes: 0,
            metrics: BTreeMap::new(),
            input_digest: input_digest.into(),
            accepted_input: serde_json::Value::Null,
        }
    }
}

/// One fetched job: its record and immutable artifact payloads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredJob {
    /// Authoritative job record.
    pub record: JobRecord,
    /// Artifact name (relative to the job prefix) to payload bytes.
    pub artifacts: BTreeMap<String, Vec<u8>>,
}

/// Artifact inventory is metadata; listing it must not download result files.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactInfo {
    pub name: String,
    pub uri: String,
    pub sha256: String,
    pub size_bytes: u64,
}

/// The outcome of an atomic admission.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Admission {
    /// The job was created by this call; dispatch may proceed.
    Created(JobRecord),
    /// A job with the same accepted input already exists and is returned
    /// unchanged; the caller must not dispatch again.
    Joined(JobRecord),
}

impl Admission {
    /// The authoritative record for this admission.
    #[must_use]
    pub fn record(&self) -> &JobRecord {
        match self {
            Self::Created(record) | Self::Joined(record) => record,
        }
    }

    /// Whether this admission created the job.
    #[must_use]
    pub fn created(&self) -> bool {
        matches!(self, Self::Created(_))
    }
}

/// Failures surfaced by a job store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoreError {
    /// A storage failure, distinct from losing application execution ownership.
    Storage {
        kind: StorageFailureKind,
        operation: &'static str,
        path: String,
        message: String,
    },
    /// The authoritative ownership token changed or its deadline expired.
    OwnershipLost(String),
    /// A successful job must carry at least one artifact.
    EmptyArtifacts,
    /// A job identifier or artifact name was not a safe path segment.
    InvalidName(String),
    /// The job does not exist.
    JobNotFound(String),
    /// The same job ID was submitted with different accepted input.
    Conflict(String),
    /// A terminal job result cannot be replaced.
    Terminal(String),
    /// A durable-store filesystem operation failed.
    Io(String),
}

impl fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Storage {
                kind,
                operation,
                path,
                message,
            } => write!(
                formatter,
                "storage {kind:?} during {operation} at {path}: {message}"
            ),
            Self::OwnershipLost(resource) => {
                write!(formatter, "execution ownership lost: {resource}")
            }
            Self::EmptyArtifacts => {
                formatter.write_str("a successful job must contain at least one artifact")
            }
            Self::InvalidName(name) => {
                write!(formatter, "{name} must be a single safe path segment")
            }
            Self::JobNotFound(job_id) => write!(formatter, "job not found: {job_id}"),
            Self::Conflict(job_id) => {
                write!(
                    formatter,
                    "job {job_id} already exists with different input"
                )
            }
            Self::Terminal(job_id) => {
                write!(formatter, "job {job_id} already has a terminal result")
            }
            Self::Io(message) => write!(formatter, "{message}"),
        }
    }
}

impl std::error::Error for StoreError {}

/// Transport-neutral storage categories retained by worker supervision.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StorageFailureKind {
    Contention,
    Transient,
    Permission,
    Permanent,
}

/// Durable boundary for one authoritative job record and its artifacts.
pub trait JobStore: Send + Sync {
    /// Return one job record.
    fn get(&self, job_id: &str) -> Option<JobRecord>;
    fn try_get(&self, job_id: &str) -> Result<Option<JobRecord>, StoreError> {
        Ok(self.get(job_id))
    }
    fn try_list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy: Option<&str>,
    ) -> Result<Vec<JobRecord>, StoreError> {
        Ok(self.list(kind, state, strategy))
    }
    fn artifacts(&self, job_id: &str) -> Result<Option<Vec<ArtifactInfo>>, StoreError> {
        self.fetch(job_id)
            .map(|job| {
                let prefix = artifact_prefix(job_id)?;
                Ok(job
                    .artifacts
                    .into_iter()
                    .map(|(name, payload)| ArtifactInfo {
                        uri: format!("{prefix}/{name}"),
                        sha256: artifact_digest(&payload),
                        size_bytes: payload.len() as u64,
                        name,
                    })
                    .collect())
            })
            .transpose()
    }
    fn artifact(&self, job_id: &str, name: &str) -> Result<Option<Vec<u8>>, StoreError> {
        validate_artifact_name(name)?;
        Ok(self
            .fetch(job_id)
            .and_then(|mut job| job.artifacts.remove(name)))
    }
    fn open_artifact(
        &self,
        job_id: &str,
        name: &str,
    ) -> Result<Option<Box<dyn std::io::Read + Send>>, StoreError> {
        Ok(self
            .artifact(job_id, name)?
            .map(|bytes| Box::new(std::io::Cursor::new(bytes)) as Box<dyn std::io::Read + Send>))
    }
    /// Atomically create the job, or join an existing job with the same
    /// accepted-input digest. The same job ID with a different digest is a
    /// conflict; persistence failures are returned, never swallowed.
    ///
    /// # Errors
    ///
    /// Returns [`StoreError`] for an unsafe name, a digest conflict, or a
    /// persistence failure.
    fn admit(&self, record: JobRecord) -> Result<Admission, StoreError>;
    /// Persist worker start without changing the accepted input or artifacts.
    /// Repeated starts preserve the first timestamp; terminal jobs cannot start.
    ///
    /// # Errors
    /// Returns an error for an absent, terminal, unsafe, or unwritable job.
    fn mark_running(&self, job_id: &str, started: DateTime<Utc>) -> Result<JobRecord, StoreError>;
    /// Persist telemetry under the admitted identity; terminal jobs reject late updates.
    fn update_progress(
        &self,
        job_id: &str,
        snapshot: harness_contracts::progress::ExecutionProgress,
        now: DateTime<Utc>,
    ) -> Result<JobRecord, StoreError>;
    /// Publish a terminal result with its complete artifact set. A failed or
    /// cancelled job may publish terminal metadata with an empty artifact set;
    /// successful states still require at least one artifact.
    ///
    /// # Errors
    ///
    /// Returns [`StoreError::EmptyArtifacts`] when a successful state is
    /// published without an artifact, [`StoreError::Terminal`] when the job
    /// already has a terminal result, and [`StoreError::JobNotFound`] or
    /// [`StoreError::Io`] on failure.
    fn put_result(
        &self,
        record: JobRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<JobRecord, StoreError>;
    /// Publish sealed artifact files without retaining their payloads in RAM.
    fn put_files(
        &self,
        record: JobRecord,
        files: BTreeMap<String, std::path::PathBuf>,
    ) -> Result<JobRecord, StoreError> {
        let artifacts = files
            .into_iter()
            .map(|(name, path)| {
                std::fs::read(path)
                    .map(|bytes| (name, bytes))
                    .map_err(io_error)
            })
            .collect::<Result<_, _>>()?;
        self.put_result(record, artifacts)
    }
    /// List job records sorted by job ID with optional filters.
    fn list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy_id: Option<&str>,
    ) -> Vec<JobRecord>;
    /// Return one job's record together with every artifact payload.
    fn fetch(&self, job_id: &str) -> Option<StoredJob>;
    /// Remove a job's metadata and owned artifacts; returns whether anything
    /// was removed.
    fn delete(&self, job_id: &str) -> bool;
}

pub fn apply_progress(
    record: &mut JobRecord,
    snapshot: harness_contracts::progress::ExecutionProgress,
    now: DateTime<Utc>,
) -> Result<(), StoreError> {
    if record.state.is_terminal() {
        return Err(StoreError::Terminal(record.job_id.clone()));
    }
    let now = now.max(record.updated_at);
    for entry in &mut record.progress {
        if entry.snapshot.stage != snapshot.stage && entry.finished_at.is_none() {
            entry.finished_at = Some(now);
        }
    }
    let finished_at = snapshot.completed.then_some(now);
    if let Some(previous) = record.progress.iter_mut().find(|entry| {
        entry.snapshot.stage == snapshot.stage
            && entry.snapshot.symbol == snapshot.symbol
            && entry.snapshot.data_type == snapshot.data_type
            && entry.snapshot.start_date == snapshot.start_date
            && entry.snapshot.end_date == snapshot.end_date
    }) {
        previous.snapshot = snapshot;
        previous.updated_at = now;
        previous.finished_at = finished_at;
    } else {
        record.progress.push(JobProgress {
            snapshot,
            started_at: now,
            updated_at: now,
            finished_at,
        });
    }
    record.updated_at = now;
    Ok(())
}

/// Canonical terminal transition, independent of artifact transport.
pub fn commit_result(
    existing: &JobRecord,
    mut record: JobRecord,
    artifacts: &[ArtifactInfo],
) -> Result<JobRecord, StoreError> {
    if !record.state.is_terminal() {
        return Err(StoreError::Io(
            "result publication requires a terminal state".into(),
        ));
    }
    if existing.state.is_terminal() {
        return Err(StoreError::Terminal(existing.job_id.clone()));
    }
    if existing.job_id != record.job_id
        || existing.kind != record.kind
        || existing.input_digest != record.input_digest
        || existing.accepted_input != record.accepted_input
    {
        return Err(StoreError::Conflict(record.job_id));
    }
    if artifacts.is_empty() && !matches!(record.state, JobState::Failed | JobState::Cancelled) {
        return Err(StoreError::EmptyArtifacts);
    }
    let mut digests = BTreeMap::new();
    let mut total = 0u64;
    for info in artifacts {
        validate_artifact_name(&info.name)?;
        total = total
            .checked_add(info.size_bytes)
            .ok_or_else(|| StoreError::Io("artifact size overflow".into()))?;
        digests.insert(info.name.clone(), info.sha256.clone());
    }
    record.progress = existing.progress.clone();
    record.updated_at = record.updated_at.max(existing.updated_at);
    for entry in &mut record.progress {
        if record.state == JobState::Succeeded {
            entry.snapshot.completed = true;
        }
        if entry.finished_at.is_none() {
            entry.finished_at = Some(record.updated_at);
        }
    }
    record.run_digest = Some(run_digest(&record.job_id, &digests));
    record.artifact_count = digests.len() as u64;
    record.total_bytes = total;
    Ok(record)
}

/// Compose the same job telemetry port for MCP and HTTP execution callers.
pub fn progress_reporter(
    store: std::sync::Arc<dyn JobStore>,
    job_id: String,
) -> harness_contracts::progress::ProgressReporter {
    std::sync::Arc::new(move |snapshot| {
        store
            .update_progress(&job_id, snapshot, Utc::now())
            .map(|_| ())
            .map_err(|error| error.to_string())
    })
}

fn path_segment(value: &str) -> Result<&str, StoreError> {
    if value.is_empty()
        || value.len() > 128
        || value.contains('/')
        || value.contains('\\')
        || value == "."
        || value == ".."
    {
        return Err(StoreError::InvalidName(value.to_string()));
    }
    Ok(value)
}

pub fn validate_artifact_name(value: &str) -> Result<&str, StoreError> {
    if value.is_empty()
        || value.contains('\\')
        || value.starts_with('/')
        || value
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err(StoreError::InvalidName(value.to_string()));
    }
    Ok(value)
}

/// Return the blob prefix holding one job's artifacts.
///
/// # Errors
///
/// Returns [`StoreError::InvalidName`] when `job_id` is not a safe segment.
pub fn artifact_prefix(job_id: &str) -> Result<String, StoreError> {
    Ok(format!("{ARTIFACT_PREFIX}/{}", path_segment(job_id)?))
}

/// Return the SHA-256 hex digest of one artifact payload.
#[must_use]
pub fn artifact_digest(payload: &[u8]) -> String {
    hex::encode(Sha256::digest(payload))
}

/// Render a JSON value with sorted object keys and compact separators.
///
/// This reproduces the canonical JSON convention from
/// `harness.configuration.hashing.canonical_configuration_json` for the plain
/// string maps used by job result digests.
#[must_use]
pub fn canonical_json(value: &serde_json::Value) -> String {
    let mut output = String::new();
    write_canonical(value, &mut output);
    output
}

fn write_canonical(value: &serde_json::Value, output: &mut String) {
    match value {
        serde_json::Value::Object(map) => {
            let mut entries: Vec<(&String, &serde_json::Value)> = map.iter().collect();
            entries.sort_by(|left, right| left.0.cmp(right.0));
            output.push('{');
            for (index, (key, item)) in entries.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                output.push_str(&serde_json::to_string(key).unwrap_or_else(|_| "\"\"".to_string()));
                output.push(':');
                write_canonical(item, output);
            }
            output.push('}');
        }
        serde_json::Value::Array(items) => {
            output.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                write_canonical(item, output);
            }
            output.push(']');
        }
        other => output.push_str(&other.to_string()),
    }
}

/// Return the domain-separated digest over one job's artifact digests.
#[must_use]
pub fn run_digest(job_id: &str, digests: &BTreeMap<String, String>) -> String {
    let mut artifacts = serde_json::Map::new();
    for name in digests.keys() {
        if let Some(digest) = digests.get(name) {
            artifacts.insert(name.clone(), serde_json::Value::String(digest.clone()));
        }
    }
    let payload = serde_json::json!({
        "domain": JOB_DIGEST_VERSION,
        "job_id": job_id,
        "artifacts": artifacts,
    });
    let mut hasher = Sha256::new();
    hasher.update(JOB_DIGEST_DOMAIN);
    hasher.update(canonical_json(&payload).as_bytes());
    hex::encode(hasher.finalize())
}

#[derive(Default)]
struct InMemoryInner {
    blobs: BTreeMap<String, Vec<u8>>,
    jobs: BTreeMap<String, JobRecord>,
}

/// Deterministic in-memory job store for tests and local tooling.
#[derive(Default)]
pub struct InMemoryJobStore {
    inner: Mutex<InMemoryInner>,
}

impl InMemoryJobStore {
    /// Construct an empty store.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> MutexGuard<'_, InMemoryInner> {
        self.inner.lock().unwrap_or_else(|error| error.into_inner())
    }
}

impl JobStore for InMemoryJobStore {
    fn update_progress(
        &self,
        job_id: &str,
        snapshot: harness_contracts::progress::ExecutionProgress,
        now: DateTime<Utc>,
    ) -> Result<JobRecord, StoreError> {
        path_segment(job_id)?;
        let mut inner = self.lock();
        let record = inner
            .jobs
            .get_mut(job_id)
            .ok_or_else(|| StoreError::JobNotFound(job_id.to_string()))?;
        apply_progress(record, snapshot, now)?;
        Ok(record.clone())
    }

    fn mark_running(&self, job_id: &str, started: DateTime<Utc>) -> Result<JobRecord, StoreError> {
        path_segment(job_id)?;
        let mut inner = self.lock();
        let record = inner
            .jobs
            .get_mut(job_id)
            .ok_or_else(|| StoreError::JobNotFound(job_id.to_string()))?;
        if record.state.is_terminal() {
            return Err(StoreError::Terminal(job_id.to_string()));
        }
        if record.state == JobState::Queued {
            record.state = JobState::Running;
            record.updated_at = started.max(record.updated_at);
        }
        Ok(record.clone())
    }

    fn get(&self, job_id: &str) -> Option<JobRecord> {
        path_segment(job_id).ok()?;
        let inner = self.lock();
        inner.jobs.get(job_id).cloned()
    }

    fn admit(&self, record: JobRecord) -> Result<Admission, StoreError> {
        let job_id = path_segment(&record.job_id)?.to_string();
        let mut inner = self.lock();
        if let Some(existing) = inner.jobs.get(&job_id) {
            if existing.input_digest == record.input_digest
                && existing.accepted_input == record.accepted_input
            {
                return Ok(Admission::Joined(existing.clone()));
            }
            return Err(StoreError::Conflict(job_id));
        }
        inner.jobs.insert(job_id, record.clone());
        Ok(Admission::Created(record))
    }

    fn put_result(
        &self,
        record: JobRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<JobRecord, StoreError> {
        let id = path_segment(&record.job_id)?.to_string();
        let inventory: Vec<_> = artifacts
            .iter()
            .map(|(name, bytes)| ArtifactInfo {
                name: name.clone(),
                uri: String::new(),
                sha256: artifact_digest(bytes),
                size_bytes: bytes.len() as u64,
            })
            .collect();
        let mut inner = self.lock();
        let existing = inner
            .jobs
            .get(&id)
            .ok_or_else(|| StoreError::JobNotFound(id.clone()))?;
        let updated = commit_result(existing, record, &inventory)?;
        for (name, bytes) in artifacts {
            inner
                .blobs
                .insert(format!("{ARTIFACT_PREFIX}/{id}/{name}"), bytes);
        }
        inner.jobs.insert(id, updated.clone());
        Ok(updated)
    }

    fn list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy_id: Option<&str>,
    ) -> Vec<JobRecord> {
        let inner = self.lock();
        inner
            .jobs
            .values()
            .filter(|record| kind.is_none_or(|expected| record.kind == expected))
            .filter(|record| state.is_none_or(|expected| record.state == expected))
            .filter(|record| strategy_id.is_none_or(|expected| record.strategy_id == expected))
            .cloned()
            .collect()
    }

    fn fetch(&self, job_id: &str) -> Option<StoredJob> {
        let record = self.get(job_id)?;
        let prefix = format!("{ARTIFACT_PREFIX}/{}/", record.job_id);
        let inner = self.lock();
        let artifacts = inner
            .blobs
            .range(prefix.clone()..)
            .take_while(|(path, _)| path.starts_with(&prefix))
            .map(|(path, payload)| {
                (
                    path.strip_prefix(&prefix).unwrap_or(path).to_string(),
                    payload.clone(),
                )
            })
            .collect();
        Some(StoredJob { record, artifacts })
    }

    fn delete(&self, job_id: &str) -> bool {
        if path_segment(job_id).is_err() {
            return false;
        }
        let prefix = format!("{ARTIFACT_PREFIX}/{job_id}/");
        let mut inner = self.lock();
        let removed_blobs = inner
            .blobs
            .keys()
            .filter(|path| path.starts_with(&prefix))
            .count();
        inner.blobs.retain(|path, _| !path.starts_with(&prefix));
        let removed_job = inner.jobs.remove(job_id).is_some();
        removed_blobs > 0 || removed_job
    }
}

fn io_error(error: std::io::Error) -> StoreError {
    StoreError::Io(error.to_string())
}

/// Filesystem-backed job store that survives process restarts.
///
/// Layout under the configured root:
///
/// * `<root>/jobs/<job_id>.json` — authoritative job records.
/// * `<root>/objects/jobs/<job_id>/<artifact>` — artifact payloads.
///
/// The store is safe to reconstruct from the same root in a new process; every
/// query reads directly from disk.
#[derive(Debug)]
pub struct FileSystemJobStore {
    root: PathBuf,
    lock: Mutex<()>,
}

impl FileSystemJobStore {
    /// Compose a store rooted at `root`, creating no directories until write.
    #[must_use]
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self {
            root: root.into(),
            lock: Mutex::new(()),
        }
    }

    /// The configured root directory.
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    fn jobs_dir(&self) -> PathBuf {
        self.root.join("jobs")
    }

    fn job_object_dir(&self, job_id: &str) -> PathBuf {
        self.root.join("objects").join(ARTIFACT_PREFIX).join(job_id)
    }

    fn job_path(&self, job_id: &str) -> PathBuf {
        self.jobs_dir().join(format!("{job_id}.json"))
    }

    fn read_job(&self, job_id: &str) -> Result<Option<JobRecord>, StoreError> {
        let path = self.job_path(job_id);
        let text = match fs::read_to_string(&path) {
            Ok(text) => text,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(io_error(error)),
        };
        serde_json::from_str(&text)
            .map(Some)
            .map_err(|error| StoreError::Io(error.to_string()))
    }

    fn collect_artifacts(base: &Path, directory: &Path, output: &mut BTreeMap<String, Vec<u8>>) {
        let entries = match fs::read_dir(directory) {
            Ok(entries) => entries,
            Err(_) => return,
        };
        let mut paths: Vec<PathBuf> = entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .collect();
        paths.sort();
        for path in paths {
            if path.is_dir() {
                Self::collect_artifacts(base, &path, output);
            } else if let Ok(payload) = fs::read(&path) {
                if let Ok(relative) = path.strip_prefix(base) {
                    let name = relative.to_string_lossy().replace('\\', "/");
                    output.insert(name, payload);
                }
            }
        }
    }
}

impl JobStore for FileSystemJobStore {
    fn update_progress(
        &self,
        job_id: &str,
        snapshot: harness_contracts::progress::ExecutionProgress,
        now: DateTime<Utc>,
    ) -> Result<JobRecord, StoreError> {
        path_segment(job_id)?;
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let mut record = self
            .read_job(job_id)?
            .ok_or_else(|| StoreError::JobNotFound(job_id.to_string()))?;
        apply_progress(&mut record, snapshot, now)?;
        let text = serde_json::to_string_pretty(&record)
            .map_err(|error| StoreError::Io(error.to_string()))?;
        // Readers must never observe a partially rewritten progress snapshot.
        let temporary = self.job_path(job_id).with_extension("json.tmp");
        fs::write(&temporary, text).map_err(io_error)?;
        fs::rename(temporary, self.job_path(job_id)).map_err(io_error)?;
        Ok(record)
    }

    fn mark_running(&self, job_id: &str, started: DateTime<Utc>) -> Result<JobRecord, StoreError> {
        path_segment(job_id)?;
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let mut record = self
            .read_job(job_id)?
            .ok_or_else(|| StoreError::JobNotFound(job_id.to_string()))?;
        if record.state.is_terminal() {
            return Err(StoreError::Terminal(job_id.to_string()));
        }
        if record.state == JobState::Queued {
            record.state = JobState::Running;
            record.updated_at = started.max(record.updated_at);
            let text = serde_json::to_string_pretty(&record)
                .map_err(|error| StoreError::Io(error.to_string()))?;
            fs::write(self.job_path(job_id), text).map_err(io_error)?;
        }
        Ok(record)
    }

    fn get(&self, job_id: &str) -> Option<JobRecord> {
        path_segment(job_id).ok()?;
        self.read_job(job_id).ok().flatten()
    }

    fn admit(&self, record: JobRecord) -> Result<Admission, StoreError> {
        let job_id = path_segment(&record.job_id)?.to_string();
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        fs::create_dir_all(self.jobs_dir()).map_err(io_error)?;
        if let Some(existing) = self.read_job(&job_id)? {
            if existing.input_digest == record.input_digest
                && existing.accepted_input == record.accepted_input
            {
                return Ok(Admission::Joined(existing));
            }
            return Err(StoreError::Conflict(job_id));
        }
        let text = serde_json::to_string_pretty(&record)
            .map_err(|error| StoreError::Io(error.to_string()))?;
        fs::write(self.job_path(&job_id), text).map_err(io_error)?;
        Ok(Admission::Created(record))
    }

    fn put_result(
        &self,
        record: JobRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<JobRecord, StoreError> {
        let id = path_segment(&record.job_id)?.to_string();
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let existing = self
            .read_job(&id)?
            .ok_or_else(|| StoreError::JobNotFound(id.clone()))?;
        let inventory: Vec<_> = artifacts
            .iter()
            .map(|(name, bytes)| ArtifactInfo {
                name: name.clone(),
                uri: String::new(),
                sha256: artifact_digest(bytes),
                size_bytes: bytes.len() as u64,
            })
            .collect();
        let updated = commit_result(&existing, record, &inventory)?;
        for (name, bytes) in artifacts {
            let target = self.job_object_dir(&id).join(name);
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(io_error)?;
            }
            fs::write(target, bytes).map_err(io_error)?;
        }
        fs::create_dir_all(self.jobs_dir()).map_err(io_error)?;
        let temporary = self.job_path(&id).with_extension("json.tmp");
        fs::write(
            &temporary,
            serde_json::to_vec_pretty(&updated)
                .map_err(|error| StoreError::Io(error.to_string()))?,
        )
        .map_err(io_error)?;
        fs::rename(temporary, self.job_path(&id)).map_err(io_error)?;
        Ok(updated)
    }

    fn list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy_id: Option<&str>,
    ) -> Vec<JobRecord> {
        let entries = match fs::read_dir(self.jobs_dir()) {
            Ok(entries) => entries,
            Err(_) => return Vec::new(),
        };
        let mut paths: Vec<PathBuf> = entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
            .collect();
        paths.sort();
        let mut records = Vec::new();
        for path in paths {
            let Ok(text) = fs::read_to_string(&path) else {
                continue;
            };
            let Ok(record) = serde_json::from_str::<JobRecord>(&text) else {
                continue;
            };
            if kind.is_some_and(|expected| record.kind != expected) {
                continue;
            }
            if state.is_some_and(|expected| record.state != expected) {
                continue;
            }
            if strategy_id.is_some_and(|expected| record.strategy_id != expected) {
                continue;
            }
            records.push(record);
        }
        records
    }

    fn fetch(&self, job_id: &str) -> Option<StoredJob> {
        let record = self.get(job_id)?;
        let directory = self.job_object_dir(&record.job_id);
        let mut artifacts = BTreeMap::new();
        Self::collect_artifacts(&directory, &directory, &mut artifacts);
        Some(StoredJob { record, artifacts })
    }

    fn delete(&self, job_id: &str) -> bool {
        if path_segment(job_id).is_err() {
            return false;
        }
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let mut removed = false;
        let directory = self.job_object_dir(job_id);
        if directory.exists() && fs::remove_dir_all(&directory).is_ok() {
            removed = true;
        }
        let job = self.job_path(job_id);
        if job.exists() && fs::remove_file(&job).is_ok() {
            removed = true;
        }
        removed
    }
}

/// Issues read-only SAS URLs for stored artifact object paths.
pub trait SasIssuer: Send + Sync {
    /// Issue a read URL for one object path.
    fn issue(&self, object_path: &str) -> String;
}
