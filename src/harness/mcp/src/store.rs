//! Shared results and job persistence for the harness HTTP and MCP surfaces.
//!
//! This module is the single durable-store definition used by both the axum
//! HTTP boundary (`api::http::store`) and the MCP server (`crate::tools`). It
//! mirrors `harness.results.store` and `lake.catalog` closely enough for the
//! launch and run-query routes to behave identically: catalog rows carry the
//! same fields, run digests are domain-separated SHA-256 over canonical JSON,
//! and the in-memory adapters keep tests free of filesystem access while the
//! filesystem adapters survive process restarts.

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Blob prefix holding one run's artifacts.
pub const RUN_PREFIX: &str = "backtests";
const RUN_DIGEST_DOMAIN: &[u8] = b"fca:harness-results:run:v1\0";
const RUN_DIGEST_VERSION: &str = "harness.results.run.v1";

/// Lifecycle states for one stored backtest run.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RunStatus {
    /// Accepted but not started.
    Queued,
    /// Currently executing.
    Running,
    /// Finished successfully.
    Complete,
    /// Finished with an error.
    Failed,
}

/// Lifecycle states used by asynchronous launch jobs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum JobState {
    /// Accepted but not started.
    Queued,
    /// Currently executing.
    Running,
    /// Finished successfully.
    Complete,
    /// Finished successfully (job vocabulary alias).
    Succeeded,
    /// Finished with an error.
    Failed,
    /// Cancelled before completion.
    Cancelled,
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

impl RunStatus {
    /// Parse the wire spelling of a run status.
    ///
    /// # Errors
    ///
    /// Returns [`InvalidStatus`] for any unrecognized value.
    pub fn parse(value: &str) -> Result<Self, InvalidStatus> {
        match value {
            "queued" => Ok(Self::Queued),
            "running" => Ok(Self::Running),
            "complete" => Ok(Self::Complete),
            "failed" => Ok(Self::Failed),
            other => Err(InvalidStatus(other.to_string())),
        }
    }

    /// The wire spelling of this status.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Complete => "complete",
            Self::Failed => "failed",
        }
    }
}

/// Catalog row for one backtest run.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunCatalogRecord {
    /// Run identifier (a single safe path segment).
    pub run_id: String,
    /// Current lifecycle state.
    pub status: RunStatus,
    /// Identifier of the strategy (or comma-joined instance identifiers).
    pub strategy_id: String,
    /// First session date, when known.
    pub data_start: Option<NaiveDate>,
    /// Last session date, when known.
    pub data_end: Option<NaiveDate>,
    /// Domain-separated digest over the run's artifact digests.
    pub run_digest: Option<String>,
    /// Hash of the resolved configuration that produced the run.
    pub configuration_digest: Option<String>,
    /// Number of stored artifacts.
    pub artifact_count: u64,
    /// Total bytes stored under the run prefix.
    pub total_bytes: u64,
    /// Headline metrics, kept string-exact.
    pub metrics: BTreeMap<String, String>,
}

impl RunCatalogRecord {
    /// Construct a run row with empty derived fields.
    #[must_use]
    pub fn new(
        run_id: impl Into<String>,
        status: RunStatus,
        strategy_id: impl Into<String>,
    ) -> Self {
        Self {
            run_id: run_id.into(),
            status,
            strategy_id: strategy_id.into(),
            data_start: None,
            data_end: None,
            run_digest: None,
            configuration_digest: None,
            artifact_count: 0,
            total_bytes: 0,
            metrics: BTreeMap::new(),
        }
    }
}

/// One fetched run: its catalog row and immutable artifact payloads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredRun {
    /// Catalog row.
    pub record: RunCatalogRecord,
    /// Artifact name (relative to the run prefix) to payload bytes.
    pub artifacts: BTreeMap<String, Vec<u8>>,
}

/// State and audit timestamps for one catalog/ensure job.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JobRecord {
    /// Job identifier.
    pub job_id: String,
    /// Current lifecycle state.
    pub state: JobState,
    /// Creation timestamp.
    pub created_at: DateTime<Utc>,
    /// Last update timestamp.
    pub updated_at: DateTime<Utc>,
    /// Failure message, when the job failed.
    pub error_message: Option<String>,
}

impl JobRecord {
    /// Construct a job record.
    #[must_use]
    pub fn new(
        job_id: impl Into<String>,
        state: JobState,
        created_at: DateTime<Utc>,
        updated_at: DateTime<Utc>,
    ) -> Self {
        Self {
            job_id: job_id.into(),
            state,
            created_at,
            updated_at,
            error_message: None,
        }
    }
}

/// Failures surfaced by a results store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoreError {
    /// A run must carry at least one artifact.
    EmptyArtifacts,
    /// A run identifier or artifact name was not a safe path segment.
    InvalidName(String),
    /// The run does not exist.
    RunNotFound(String),
    /// A durable-store filesystem operation failed.
    Io(String),
}

impl fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyArtifacts => formatter.write_str("a run must contain at least one artifact"),
            Self::InvalidName(name) => {
                write!(formatter, "{name} must be a single safe path segment")
            }
            Self::RunNotFound(run_id) => write!(formatter, "run not found: {run_id}"),
            Self::Io(message) => write!(formatter, "{message}"),
        }
    }
}

impl std::error::Error for StoreError {}

/// Blob and catalog boundary for run artifacts and rows.
pub trait RunStore: Send + Sync {
    /// Replace the run's artifacts and upsert its catalog row.
    ///
    /// # Errors
    ///
    /// Returns [`StoreError`] when the run has no artifacts or a name is unsafe.
    fn put(
        &self,
        record: RunCatalogRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<RunCatalogRecord, StoreError>;
    /// Return one run's catalog row.
    fn get(&self, run_id: &str) -> Option<RunCatalogRecord>;
    /// List catalog rows sorted by run ID with optional filters.
    fn list(&self, status: Option<RunStatus>, strategy_id: Option<&str>) -> Vec<RunCatalogRecord>;
    /// Return one run's row together with every artifact payload.
    fn fetch(&self, run_id: &str) -> Option<StoredRun>;
    /// Remove a run's artifacts and row; returns whether anything was removed.
    fn delete(&self, run_id: &str) -> bool;
}

/// Catalog boundary for asynchronous launch jobs.
pub trait JobRepository: Send + Sync {
    /// Return one job record.
    fn get(&self, job_id: &str) -> Option<JobRecord>;
    /// Insert or replace one job record.
    fn upsert(&self, record: JobRecord) -> JobRecord;
}

fn path_segment(value: &str) -> Result<&str, StoreError> {
    if value.is_empty()
        || value.contains('/')
        || value.contains('\\')
        || value == "."
        || value == ".."
    {
        return Err(StoreError::InvalidName(value.to_string()));
    }
    Ok(value)
}

fn object_path(value: &str) -> Result<&str, StoreError> {
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

/// Return the blob prefix holding one run's artifacts.
///
/// # Errors
///
/// Returns [`StoreError::InvalidName`] when `run_id` is not a safe segment.
pub fn run_prefix(run_id: &str) -> Result<String, StoreError> {
    Ok(format!("{RUN_PREFIX}/{}", path_segment(run_id)?))
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
/// string maps used by run digests.
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

/// Return the domain-separated digest over one run's artifact digests.
#[must_use]
pub fn run_digest(run_id: &str, digests: &BTreeMap<String, String>) -> String {
    let mut artifacts = serde_json::Map::new();
    for name in digests.keys() {
        if let Some(digest) = digests.get(name) {
            artifacts.insert(name.clone(), serde_json::Value::String(digest.clone()));
        }
    }
    let payload = serde_json::json!({
        "domain": RUN_DIGEST_VERSION,
        "run_id": run_id,
        "artifacts": artifacts,
    });
    let mut hasher = Sha256::new();
    hasher.update(RUN_DIGEST_DOMAIN);
    hasher.update(canonical_json(&payload).as_bytes());
    hex::encode(hasher.finalize())
}

#[derive(Default)]
struct InMemoryInner {
    blobs: BTreeMap<String, Vec<u8>>,
    catalog: BTreeMap<String, RunCatalogRecord>,
}

/// Deterministic in-memory results store for tests and local tooling.
#[derive(Default)]
pub struct InMemoryRunStore {
    inner: Mutex<InMemoryInner>,
}

impl InMemoryRunStore {
    /// Construct an empty store.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }
}

impl RunStore for InMemoryRunStore {
    fn put(
        &self,
        record: RunCatalogRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<RunCatalogRecord, StoreError> {
        if artifacts.is_empty() {
            return Err(StoreError::EmptyArtifacts);
        }
        let run_id = path_segment(&record.run_id)?.to_string();
        let prefix = format!("{RUN_PREFIX}/{run_id}/");
        let mut prepared: BTreeMap<String, Vec<u8>> = BTreeMap::new();
        let mut digests: BTreeMap<String, String> = BTreeMap::new();
        let mut total_bytes: u64 = 0;
        for (name, payload) in artifacts {
            let safe_name = object_path(&name)?.to_string();
            digests.insert(safe_name.clone(), artifact_digest(&payload));
            total_bytes += payload.len() as u64;
            prepared.insert(format!("{prefix}{safe_name}"), payload);
        }
        let mut inner = self.inner.lock().expect("results store poisoned");
        inner.blobs.retain(|path, _| !path.starts_with(&prefix));
        for (path, payload) in prepared {
            inner.blobs.insert(path, payload);
        }
        let mut updated = record;
        updated.run_digest = Some(run_digest(&run_id, &digests));
        updated.artifact_count = digests.len() as u64;
        updated.total_bytes = total_bytes;
        inner.catalog.insert(run_id, updated.clone());
        Ok(updated)
    }

    fn get(&self, run_id: &str) -> Option<RunCatalogRecord> {
        path_segment(run_id).ok()?;
        let inner = self.inner.lock().expect("results store poisoned");
        inner.catalog.get(run_id).cloned()
    }

    fn list(&self, status: Option<RunStatus>, strategy_id: Option<&str>) -> Vec<RunCatalogRecord> {
        let inner = self.inner.lock().expect("results store poisoned");
        inner
            .catalog
            .values()
            .filter(|record| status.is_none_or(|expected| record.status == expected))
            .filter(|record| strategy_id.is_none_or(|expected| record.strategy_id == expected))
            .cloned()
            .collect()
    }

    fn fetch(&self, run_id: &str) -> Option<StoredRun> {
        let record = self.get(run_id)?;
        let prefix = format!("{RUN_PREFIX}/{}/", record.run_id);
        let inner = self.inner.lock().expect("results store poisoned");
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
        Some(StoredRun { record, artifacts })
    }

    fn delete(&self, run_id: &str) -> bool {
        if path_segment(run_id).is_err() {
            return false;
        }
        let prefix = format!("{RUN_PREFIX}/{run_id}/");
        let mut inner = self.inner.lock().expect("results store poisoned");
        let removed_blobs = inner
            .blobs
            .keys()
            .filter(|path| path.starts_with(&prefix))
            .count();
        inner.blobs.retain(|path, _| !path.starts_with(&prefix));
        let removed_row = inner.catalog.remove(run_id).is_some();
        removed_blobs > 0 || removed_row
    }
}

/// Deterministic in-memory job repository for tests and local tooling.
#[derive(Default)]
pub struct InMemoryJobRepository {
    inner: Mutex<BTreeMap<String, JobRecord>>,
}

impl InMemoryJobRepository {
    /// Construct an empty repository.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }
}

impl JobRepository for InMemoryJobRepository {
    fn get(&self, job_id: &str) -> Option<JobRecord> {
        let inner = self.inner.lock().expect("job repository poisoned");
        inner.get(job_id).cloned()
    }

    fn upsert(&self, record: JobRecord) -> JobRecord {
        let mut inner = self.inner.lock().expect("job repository poisoned");
        inner.insert(record.job_id.clone(), record.clone());
        record
    }
}

fn io_error(error: std::io::Error) -> StoreError {
    StoreError::Io(error.to_string())
}

/// Filesystem-backed results store that survives process restarts.
///
/// Layout under the configured root:
///
/// * `<root>/catalog/<run_id>.json` — catalog rows.
/// * `<root>/objects/backtests/<run_id>/<artifact>` — artifact payloads.
///
/// The store is safe to reconstruct from the same root in a new process; every
/// query reads directly from disk.
#[derive(Debug, Clone)]
pub struct FileSystemRunStore {
    root: PathBuf,
}

impl FileSystemRunStore {
    /// Compose a store rooted at `root`, creating no directories until write.
    #[must_use]
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    /// The configured root directory.
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    fn catalog_dir(&self) -> PathBuf {
        self.root.join("catalog")
    }

    fn run_object_dir(&self, run_id: &str) -> PathBuf {
        self.root.join("objects").join(RUN_PREFIX).join(run_id)
    }

    fn catalog_path(&self, run_id: &str) -> PathBuf {
        self.catalog_dir().join(format!("{run_id}.json"))
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

impl RunStore for FileSystemRunStore {
    fn put(
        &self,
        record: RunCatalogRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<RunCatalogRecord, StoreError> {
        if artifacts.is_empty() {
            return Err(StoreError::EmptyArtifacts);
        }
        let run_id = path_segment(&record.run_id)?.to_string();
        let directory = self.run_object_dir(&run_id);
        if directory.exists() {
            fs::remove_dir_all(&directory).map_err(io_error)?;
        }
        fs::create_dir_all(&directory).map_err(io_error)?;
        let mut digests: BTreeMap<String, String> = BTreeMap::new();
        let mut total_bytes: u64 = 0;
        for (name, payload) in artifacts {
            let safe_name = object_path(&name)?.to_string();
            let target = directory.join(&safe_name);
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(io_error)?;
            }
            fs::write(&target, &payload).map_err(io_error)?;
            digests.insert(safe_name, artifact_digest(&payload));
            total_bytes += payload.len() as u64;
        }
        let mut updated = record;
        updated.run_digest = Some(run_digest(&run_id, &digests));
        updated.artifact_count = digests.len() as u64;
        updated.total_bytes = total_bytes;
        fs::create_dir_all(self.catalog_dir()).map_err(io_error)?;
        let text = serde_json::to_string_pretty(&updated)
            .map_err(|error| StoreError::Io(error.to_string()))?;
        fs::write(self.catalog_path(&run_id), text).map_err(io_error)?;
        Ok(updated)
    }

    fn get(&self, run_id: &str) -> Option<RunCatalogRecord> {
        path_segment(run_id).ok()?;
        let text = fs::read_to_string(self.catalog_path(run_id)).ok()?;
        serde_json::from_str(&text).ok()
    }

    fn list(&self, status: Option<RunStatus>, strategy_id: Option<&str>) -> Vec<RunCatalogRecord> {
        let entries = match fs::read_dir(self.catalog_dir()) {
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
            let Ok(record) = serde_json::from_str::<RunCatalogRecord>(&text) else {
                continue;
            };
            if status.is_some_and(|expected| record.status != expected) {
                continue;
            }
            if strategy_id.is_some_and(|expected| record.strategy_id != expected) {
                continue;
            }
            records.push(record);
        }
        records
    }

    fn fetch(&self, run_id: &str) -> Option<StoredRun> {
        let record = self.get(run_id)?;
        let directory = self.run_object_dir(&record.run_id);
        let mut artifacts = BTreeMap::new();
        Self::collect_artifacts(&directory, &directory, &mut artifacts);
        Some(StoredRun { record, artifacts })
    }

    fn delete(&self, run_id: &str) -> bool {
        if path_segment(run_id).is_err() {
            return false;
        }
        let mut removed = false;
        let directory = self.run_object_dir(run_id);
        if directory.exists() && fs::remove_dir_all(&directory).is_ok() {
            removed = true;
        }
        let catalog = self.catalog_path(run_id);
        if catalog.exists() && fs::remove_file(&catalog).is_ok() {
            removed = true;
        }
        removed
    }
}

/// Filesystem-backed job repository that survives process restarts.
///
/// Each job is a JSON document under `<root>/jobs/<job_id>.json`.
#[derive(Debug, Clone)]
pub struct FileSystemJobRepository {
    root: PathBuf,
}

impl FileSystemJobRepository {
    /// Compose a repository rooted at `root`.
    #[must_use]
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    /// The configured root directory.
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    fn jobs_dir(&self) -> PathBuf {
        self.root.join("jobs")
    }

    fn job_path(&self, job_id: &str) -> PathBuf {
        self.jobs_dir().join(format!("{job_id}.json"))
    }
}

impl JobRepository for FileSystemJobRepository {
    fn get(&self, job_id: &str) -> Option<JobRecord> {
        path_segment(job_id).ok()?;
        let text = fs::read_to_string(self.job_path(job_id)).ok()?;
        serde_json::from_str(&text).ok()
    }

    fn upsert(&self, record: JobRecord) -> JobRecord {
        if path_segment(&record.job_id).is_err() {
            return record;
        }
        if fs::create_dir_all(self.jobs_dir()).is_err() {
            return record;
        }
        if let Ok(text) = serde_json::to_string_pretty(&record) {
            let _ = fs::write(self.job_path(&record.job_id), text);
        }
        record
    }
}

/// Issues read-only SAS URLs for stored artifact object paths.
pub trait SasIssuer: Send + Sync {
    /// Issue a read URL for one object path.
    fn issue(&self, object_path: &str) -> String;
}

#[cfg(test)]
#[path = "../tests/unit/store.rs"]
mod tests;
