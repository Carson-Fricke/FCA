//! Parallel sweep orchestration over an injected job-launcher port.
//!
//! Port of `harness.sweeps.runner`. The runner expands a
//! [`SweepSpec`](super::spec::SweepSpec) into deduplicated points, persists one
//! sweep record mapping the sweep ID to its run IDs, and submits every derived
//! configuration through the injected [`SweepJobLauncher`]. Parallelism is the
//! launcher's concern, so per-run output stays deterministic regardless of
//! scheduling.

use std::collections::BTreeMap;

use crate::canonical_json::JsonValue;
use crate::runtime::state_store::parse_json;

use super::spec::{SweepPoint, SweepSpec};

const SWEEP_PREFIX: &str = "sweeps";
const RECORD_NAME: &str = "sweep.json";
const RECORD_VERSION: &str = "harness.sweeps.record.v1";
const HEXA_DIGEST: usize = 64;

/// Submission boundary for one derived sweep run.
///
/// Synchronous enqueue semantics: implementations schedule the run and return
/// without waiting for completion.
pub trait SweepJobLauncher {
    /// Enqueue one derived run.
    fn launch(&self, job_id: &str);
}

/// Whole-object storage boundary for sweep records.
pub trait RunBlobStore {
    /// Atomically replace the object at a relative path.
    fn replace(&mut self, relative_path: &str, payload: &[u8]);
    /// Return the full payload of one object, or `None` when absent.
    fn read(&self, relative_path: &str) -> Option<Vec<u8>>;
    /// Return every stored path with the given prefix, sorted.
    fn list_prefix(&self, prefix: &str) -> Vec<String>;
}

/// One catalog row the runner joins sweep runs against.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CatalogRecord {
    /// Run identifier.
    pub job_id: String,
    /// Terminal or in-flight status.
    pub status: RunStatus,
    /// String-exact headline metrics.
    pub metrics: BTreeMap<String, String>,
}

/// Lifecycle state of one stored run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunStatus {
    /// Enqueued but not started.
    Queued,
    /// Currently running.
    Running,
    /// Finished successfully.
    Complete,
    /// Finished with a failure.
    Failed,
}

/// Run catalog lookup.
pub trait RunCatalog {
    /// Return one catalog row, or `None` when the run is not cataloged.
    fn get(&self, job_id: &str) -> Option<CatalogRecord>;
}

/// In-memory blob store for deterministic tests and fakes.
#[derive(Clone, Debug, Default)]
pub struct InMemoryRunBlobStore {
    objects: BTreeMap<String, Vec<u8>>,
}

impl InMemoryRunBlobStore {
    /// Create an empty store.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }
}

impl RunBlobStore for InMemoryRunBlobStore {
    fn replace(&mut self, relative_path: &str, payload: &[u8]) {
        self.objects
            .insert(relative_path.to_string(), payload.to_vec());
    }

    fn read(&self, relative_path: &str) -> Option<Vec<u8>> {
        self.objects.get(relative_path).cloned()
    }

    fn list_prefix(&self, prefix: &str) -> Vec<String> {
        let mut paths: Vec<String> = self
            .objects
            .keys()
            .filter(|path| path.starts_with(prefix))
            .cloned()
            .collect();
        paths.sort();
        paths
    }
}

/// In-memory run catalog for deterministic tests and fakes.
#[derive(Clone, Debug, Default)]
pub struct InMemoryRunCatalog {
    records: BTreeMap<String, CatalogRecord>,
}

impl InMemoryRunCatalog {
    /// Create an empty catalog.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Insert or replace one catalog row.
    pub fn insert(&mut self, record: CatalogRecord) {
        self.records.insert(record.job_id.clone(), record);
    }
}

impl RunCatalog for InMemoryRunCatalog {
    fn get(&self, job_id: &str) -> Option<CatalogRecord> {
        self.records.get(job_id).cloned()
    }
}

/// A launcher that records the run IDs it was asked to enqueue.
#[derive(Clone, Debug, Default)]
pub struct RecordingLauncher {
    launched: std::cell::RefCell<Vec<String>>,
}

impl RecordingLauncher {
    /// The launched run IDs in submission order.
    #[must_use]
    pub fn launched(&self) -> Vec<String> {
        self.launched.borrow().clone()
    }
}

impl SweepJobLauncher for RecordingLauncher {
    fn launch(&self, job_id: &str) {
        self.launched.borrow_mut().push(job_id.to_string());
    }
}

/// Return the blob prefix holding one sweep's record.
///
/// # Errors
///
/// Returns an error message when `sweep_id` is not a lowercase SHA-256 digest.
pub fn sweep_prefix(sweep_id: &str) -> Result<String, String> {
    if sweep_id.len() != HEXA_DIGEST
        || !sweep_id
            .chars()
            .all(|character| character.is_ascii_hexdigit() && !character.is_ascii_uppercase())
    {
        return Err("sweep_id must be a 64-character lowercase sha-256 hex digest".to_string());
    }
    Ok(format!("{SWEEP_PREFIX}/{sweep_id}"))
}

/// Outcome of submitting one sweep for execution.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SweepSubmission {
    /// Sweep identity.
    pub sweep_id: String,
    /// Requested draws (grid mode derives this as the product).
    pub requested: usize,
    /// Expanded, deduplicated point count.
    pub expanded: usize,
    /// Duplicates skipped during deduplication.
    pub duplicates_skipped: usize,
    /// Launched run identifiers.
    pub job_ids: Vec<String>,
    /// Expanded points.
    pub points: Vec<SweepPoint>,
}

/// One sweep run joined against the results catalog.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SweepRunView {
    /// Run identifier.
    pub job_id: String,
    /// Catalog status, when present.
    pub status: Option<RunStatus>,
    /// String-exact headline metrics.
    pub metrics: BTreeMap<String, String>,
}

/// Queryable sweep state derived from the run catalog.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SweepStatus {
    /// Sweep identity.
    pub sweep_id: String,
    /// Requested draws.
    pub requested: usize,
    /// Expanded count.
    pub expanded: usize,
    /// Per-run views.
    pub runs: Vec<SweepRunView>,
}

impl SweepStatus {
    /// Count runs the catalog recorded as complete.
    #[must_use]
    pub fn complete(&self) -> usize {
        self.runs
            .iter()
            .filter(|view| view.status == Some(RunStatus::Complete))
            .count()
    }

    /// Count runs the catalog recorded as failed.
    #[must_use]
    pub fn failed(&self) -> usize {
        self.runs
            .iter()
            .filter(|view| view.status == Some(RunStatus::Failed))
            .count()
    }

    /// Count runs not yet cataloged or still queued or running.
    #[must_use]
    pub fn pending(&self) -> usize {
        self.runs
            .iter()
            .filter(|view| {
                matches!(
                    view.status,
                    None | Some(RunStatus::Queued) | Some(RunStatus::Running)
                )
            })
            .count()
    }

    /// Whether every deduplicated run is complete and none failed.
    #[must_use]
    pub fn is_complete(&self) -> bool {
        !self.runs.is_empty() && self.pending() == 0 && self.failed() == 0
    }
}

fn record_payload(spec: &SweepSpec, points: &[SweepPoint]) -> JsonValue {
    JsonValue::object([
        ("version", JsonValue::string(RECORD_VERSION)),
        ("sweep_id", JsonValue::String(spec.sweep_id())),
        ("mode", JsonValue::string(spec.mode.value())),
        ("requested", JsonValue::UInt(spec.count as u64)),
        (
            "runs",
            JsonValue::Array(
                points
                    .iter()
                    .map(|point| {
                        JsonValue::object([
                            ("index", JsonValue::UInt(point.index as u64)),
                            ("job_id", JsonValue::String(point.job_id.clone())),
                            (
                                "configuration_digest",
                                JsonValue::String(point.configuration_digest.clone()),
                            ),
                            ("parameters", JsonValue::Object(point.parameters.clone())),
                        ])
                    })
                    .collect(),
            ),
        ),
    ])
}

/// Expand, deduplicate, submit, and track one parameter sweep.
pub struct SweepRunner {
    launcher: Box<dyn SweepJobLauncher>,
    blobs: Box<dyn RunBlobStore>,
    catalog: Box<dyn RunCatalog>,
}

impl SweepRunner {
    /// Wire the launcher port and the shared storage adapters.
    #[must_use]
    pub fn new(
        launcher: Box<dyn SweepJobLauncher>,
        blobs: Box<dyn RunBlobStore>,
        catalog: Box<dyn RunCatalog>,
    ) -> Self {
        Self {
            launcher,
            blobs,
            catalog,
        }
    }

    /// Deduplicate the spec, persist its record, and launch every point.
    ///
    /// # Errors
    ///
    /// Returns an error message when the sweep identity is malformed.
    pub fn submit(&mut self, spec: &SweepSpec) -> Result<SweepSubmission, String> {
        let points = spec.expand();
        let record_path = format!("{}/{RECORD_NAME}", sweep_prefix(&spec.sweep_id())?);
        self.blobs.replace(
            &record_path,
            record_payload(spec, &points)
                .to_canonical_string_utf8()
                .as_bytes(),
        );
        for point in &points {
            self.launcher.launch(&point.job_id);
        }
        Ok(SweepSubmission {
            sweep_id: spec.sweep_id(),
            requested: spec.count,
            expanded: points.len(),
            duplicates_skipped: spec.count - points.len(),
            job_ids: points.iter().map(|point| point.job_id.clone()).collect(),
            points,
        })
    }

    /// Return one sweep's state joined against the run catalog.
    ///
    /// # Errors
    ///
    /// Returns an error message when the sweep is unknown or its record is
    /// malformed.
    pub fn status(&self, sweep_id: &str) -> Result<SweepStatus, String> {
        let prefix = sweep_prefix(sweep_id)?;
        let key = format!("{prefix}/{RECORD_NAME}");
        let raw = self
            .blobs
            .read(&key)
            .ok_or_else(|| format!("sweep not found: {sweep_id}"))?;
        let text = String::from_utf8(raw).map_err(|error| error.to_string())?;
        let document = parse_json(&text, &key).map_err(|error| error.to_string())?;
        let JsonValue::Object(entries) = document else {
            return Err("sweep record is malformed".to_string());
        };
        let requested = match entries.get("requested") {
            Some(JsonValue::UInt(value)) => *value as usize,
            _ => return Err("sweep record is missing requested".to_string()),
        };
        let runs = match entries.get("runs") {
            Some(JsonValue::Array(items)) => items,
            _ => return Err("sweep record is missing runs".to_string()),
        };
        let mut views = Vec::with_capacity(runs.len());
        for item in runs {
            let JsonValue::Object(run) = item else {
                return Err("sweep run entry is malformed".to_string());
            };
            let Some(JsonValue::String(job_id)) = run.get("job_id") else {
                return Err("sweep run entry is missing job_id".to_string());
            };
            let view = match self.catalog.get(job_id) {
                Some(record) => SweepRunView {
                    job_id: record.job_id,
                    status: Some(record.status),
                    metrics: record.metrics,
                },
                None => SweepRunView {
                    job_id: job_id.clone(),
                    status: None,
                    metrics: BTreeMap::new(),
                },
            };
            views.push(view);
        }
        Ok(SweepStatus {
            sweep_id: sweep_id.to_string(),
            requested,
            expanded: views.len(),
            runs: views,
        })
    }

    /// Return every persisted sweep ID in sorted order.
    ///
    /// # Errors
    ///
    /// Currently infallible; reserved for adapters that surface I/O failures.
    pub fn sweep_ids(&self) -> Result<Vec<String>, String> {
        let prefix = format!("{SWEEP_PREFIX}/");
        let suffix = format!("/{RECORD_NAME}");
        let mut ids: Vec<String> = self
            .blobs
            .list_prefix(&prefix)
            .into_iter()
            .filter_map(|path| {
                if path.ends_with(&suffix) {
                    Some(path[prefix.len()..path.len() - suffix.len()].to_string())
                } else {
                    None
                }
            })
            .collect();
        ids.sort();
        Ok(ids)
    }
}
