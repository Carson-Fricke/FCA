//! Owns neutral job records, admission/publication invariants and storage capabilities.
//! No transport, filesystem or cloud implementations belong here.

use sha2::Digest;
use sha2::Sha256;
use std::collections::BTreeMap;
use std::fmt;

use chrono::DateTime;
use chrono::NaiveDate;
use chrono::Utc;
use serde::Deserialize;
use serde::Serialize;

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

/// Atomically admits a retained identity, joining identical inputs and rejecting conflicts.
pub trait JobAdmission: Send + Sync {
    fn admit(&self, record: JobRecord) -> Result<Admission, StoreError>;
}
/// Metadata-only queries. Absence is distinct from a storage failure.
pub trait JobQueries: Send + Sync {
    fn try_get(&self, id: &str) -> Result<Option<JobRecord>, StoreError>;
    fn try_list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy: Option<&str>,
    ) -> Result<Vec<JobRecord>, StoreError>;
}
/// Artifact inventory never materializes payloads; open_artifact streams verified bytes.
pub trait ArtifactReader: Send + Sync {
    fn artifacts(&self, id: &str) -> Result<Option<Vec<ArtifactInfo>>, StoreError>;
    fn artifact(&self, id: &str, name: &str) -> Result<Option<Vec<u8>>, StoreError>;
    fn open_artifact(
        &self,
        id: &str,
        name: &str,
    ) -> Result<Option<Box<dyn std::io::Read + Send>>, StoreError>;
}
/// Worker publication authority. Implementations enforce ownership and immutable admission.
pub trait ResultPublisher: Send + Sync {
    fn mark_running(&self, id: &str, now: DateTime<Utc>) -> Result<JobRecord, StoreError>;
    fn update_progress(
        &self,
        id: &str,
        progress: harness_contracts::progress::ExecutionProgress,
        now: DateTime<Utc>,
    ) -> Result<JobRecord, StoreError>;
    fn put_result(
        &self,
        record: JobRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<JobRecord, StoreError>;
    fn put_files(
        &self,
        record: JobRecord,
        files: BTreeMap<String, std::path::PathBuf>,
    ) -> Result<JobRecord, StoreError>;
}
/// Explicit retention authority; storage failure must reach the caller.
pub trait JobRetention: Send + Sync {
    fn remove(&self, id: &str) -> Result<bool, StoreError>;
}
/// Read-only composition for status and artifact consumers.
pub trait JobReader: JobQueries + ArtifactReader {}
impl<T: JobQueries + ArtifactReader + ?Sized> JobReader for T {}
/// Full composition input; consumers should retain only their required capability.
pub trait ExecutionRepository: JobReader + JobAdmission + JobExecution + JobRetention {}
impl<T: JobReader + JobAdmission + ResultPublisher + JobRetention + ?Sized> ExecutionRepository
    for T
{
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
        || existing.created_at != record.created_at
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
    store: std::sync::Arc<dyn ResultPublisher>,
    job_id: String,
) -> harness_contracts::progress::ProgressReporter {
    std::sync::Arc::new(move |snapshot| {
        store
            .update_progress(&job_id, snapshot, Utc::now())
            .map(|_| ())
            .map_err(|error| error.to_string())
    })
}

pub fn path_segment(value: &str) -> Result<&str, StoreError> {
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

/// Issues read-only SAS URLs for stored artifact object paths.
pub trait SasIssuer: Send + Sync {
    /// Issue a read URL for one object path.
    fn issue(&self, object_path: &str) -> String;
}

/// A worker may inspect and publish its execution, but cannot admit/delete jobs or read artifacts.
pub trait JobExecution: JobQueries + ResultPublisher {}
impl<T: JobQueries + ResultPublisher + ?Sized> JobExecution for T {}
