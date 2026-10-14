//! Provider-independent coverage and ensure use cases.
//!
//! Ports `lake/coverage/application.py`: request validation, the stable ensure
//! job identifier, and the ensure flow that returns satisfied coverage, reuses
//! an in-flight job, repairs a terminal job, and settles a launched job even
//! after the caller's wait deadline expires.

use std::sync::Arc;
use std::time::Duration;

use futures_util::future::BoxFuture;
use lake_catalog::{JobRecord, JobRepository, JobState};
use lake_contracts::{AssetClass, DataError, DataType, Date, DateTime};
use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::error::CoverageRequestError;
use crate::service::{calculate_coverage, CoverageReport, SessionCoverage};

/// A provider-independent coverage request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnsureSpec {
    /// Asset class of the requested window.
    pub asset_class: AssetClass,
    /// Data type of the requested window.
    pub data_type: DataType,
    /// Requested symbol, canonically uppercased by [`EnsureSpec::canonical`].
    pub symbol: String,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Canonical bars interval token; required for bars and absent otherwise.
    pub interval: Option<String>,
}

impl EnsureSpec {
    /// Return the request with its symbol stripped and uppercased once.
    pub fn canonical(&self) -> Self {
        Self {
            asset_class: self.asset_class,
            data_type: self.data_type,
            symbol: self.symbol.trim().to_uppercase(),
            start_date: self.start_date,
            end_date: self.end_date,
            interval: self.interval.clone(),
        }
    }

    /// Validate the data type / interval pairing.
    ///
    /// Bars requires exactly one canonical token; every other data type must
    /// not carry an interval.
    pub fn validate_interval(&self) -> Result<(), CoverageRequestError> {
        match (self.data_type, self.interval.as_deref()) {
            (DataType::Bars, None) => Err(CoverageRequestError::new(
                "invalid_interval",
                "bars requires an interval",
                None,
            )),
            (DataType::Bars, Some(interval)) => {
                lake_contracts::BarInterval::parse(interval).map_err(|error| {
                    CoverageRequestError::new("invalid_interval", error.to_string(), None)
                })?;
                Ok(())
            }
            (_, Some(_)) => Err(CoverageRequestError::new(
                "invalid_interval",
                "interval is supported for bars only",
                None,
            )),
            (_, None) => Ok(()),
        }
    }
}

/// Work description passed to an injected ensure launcher.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnsureJob {
    /// Stable job identifier.
    pub job_id: String,
    /// The request that produced this job.
    pub request: EnsureSpec,
    /// Actionable coverage entries the launcher must fill.
    pub gaps: Vec<SessionCoverage>,
}

/// An injected ensure launcher: it fills the job's gaps and reports success.
pub type EnsureJobLauncher =
    Arc<dyn Fn(EnsureJob) -> BoxFuture<'static, Result<(), DataError>> + Send + Sync>;

/// The result of one ensure request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EnsureOutcome {
    /// Every expected session was already terminal: no job was launched.
    Covered(CoverageReport),
    /// A persisted job carries the request: queued, running, or settled.
    Accepted(JobRecord),
}

/// Validate and calculate one coverage request.
///
/// The request covers any window, including full history: ensure exists to
/// make a period complete, and no date-range cap bounds it. An unsupported
/// asset class is rejected with the Python message variants selected by
/// `operation`: `catalog` and `query` have dedicated wording, any other
/// operation is named in the message.
pub fn requested_coverage(
    catalog: &dyn lake_catalog::CatalogRepository,
    listings: Option<&dyn lake_catalog::ListingRepository>,
    spec: &EnsureSpec,
    as_of: DateTime,
    operation: &str,
) -> Result<CoverageReport, CoverageRequestError> {
    if !matches!(
        spec.asset_class,
        AssetClass::Equity | AssetClass::Crypto | AssetClass::Option
    ) {
        let message = if operation == "query" {
            "file queries support equity and crypto only".to_string()
        } else {
            let subject = if operation == "catalog" {
                "coverage"
            } else {
                operation
            };
            format!("{subject} planning supports equity and crypto only")
        };
        return Err(CoverageRequestError::new(
            "unsupported_asset_class",
            message,
            None,
        ));
    }
    spec.validate_interval()?;
    calculate_coverage(
        catalog,
        listings,
        spec.asset_class,
        std::slice::from_ref(&spec.data_type),
        std::slice::from_ref(&spec.symbol),
        spec.interval.as_deref(),
        spec.start_date,
        spec.end_date,
        as_of,
    )
}

/// Return satisfied coverage or launch one stable asynchronous job.
///
/// A persisted job record short-circuits repeated calls while it is still in
/// flight. A terminal record whose coverage remains unsatisfied is superseded
/// and the work is relaunched, so re-ensuring the same spec repairs sessions
/// that ended in error or partial states. The record is marked running while
/// the launcher works and reaches a terminal state (`complete` or `failed`) as
/// soon as it returns, even when the caller's wait deadline expires. Ensure
/// requests are unbounded: the spec may span any window, including full
/// history.
///
/// The job repository is a shared [`Arc`] rather than a plain reference
/// because the launched work deliberately outlives this call: after the wait
/// deadline the task keeps running on the runtime and settles the job record
/// when the launcher returns, which a borrowed handle could not soundly do.
/// Launching requires a Tokio runtime; without one the request is rejected
/// with an `internal_error` instead of panicking.
pub async fn ensure_coverage(
    catalog: &dyn lake_catalog::CatalogRepository,
    jobs: &Arc<dyn JobRepository>,
    listings: Option<&dyn lake_catalog::ListingRepository>,
    launcher: EnsureJobLauncher,
    spec: &EnsureSpec,
    as_of: DateTime,
    wait_deadline_seconds: f64,
) -> Result<EnsureOutcome, CoverageRequestError> {
    let report = requested_coverage(catalog, listings, spec, as_of, "ensure")?;
    if report.is_satisfied() {
        return Ok(EnsureOutcome::Covered(report));
    }

    let job_id = ensure_job_id(spec)?;
    let existing = jobs.get(&job_id).map_err(CoverageRequestError::from)?;
    if let Some(record) = &existing {
        if matches!(record.state, JobState::Queued | JobState::Running) {
            return Ok(EnsureOutcome::Accepted(record.clone()));
        }
    }

    let runtime = tokio::runtime::Handle::try_current().map_err(|_| {
        CoverageRequestError::new(
            "internal_error",
            "ensure_coverage requires a Tokio runtime",
            None,
        )
    })?;

    let created_at = existing.map_or(as_of, |record| record.created_at);
    let accepted = JobRecord::new(job_id.clone(), JobState::Running, created_at, as_of, None)
        .map_err(CoverageRequestError::from)?;
    jobs.upsert(&accepted).map_err(CoverageRequestError::from)?;

    let job = EnsureJob {
        job_id: job_id.clone(),
        request: spec.clone(),
        gaps: report.gap_plan().gaps,
    };
    let jobs_for_task = Arc::clone(jobs);
    let task_job_id = job_id.clone();
    let handle = runtime.spawn(async move {
        let result = launcher(job).await;
        let state = if result.is_ok() {
            JobState::Complete
        } else {
            JobState::Failed
        };
        let error_message = result.as_ref().err().map(ToString::to_string);
        if let Ok(record) = JobRecord::new(task_job_id, state, created_at, as_of, error_message) {
            let _ = jobs_for_task.upsert(&record);
        }
        result
    });

    // A panicked task or an expired deadline falls through to the persisted
    // record, which the task settles itself. A launcher error has already
    // settled the record as failed; mirroring Python, the failed row is the
    // fallback when the repository no longer holds it.
    let deadline = Duration::from_secs_f64(normalized_deadline(wait_deadline_seconds));
    let mut fallback = accepted;
    match tokio::time::timeout(deadline, handle).await {
        Ok(Ok(Ok(()))) => {
            let refreshed = requested_coverage(catalog, listings, spec, as_of, "ensure")?;
            if refreshed.is_satisfied() {
                return Ok(EnsureOutcome::Covered(refreshed));
            }
        }
        Ok(Ok(Err(error))) => {
            if let Ok(record) = JobRecord::new(
                job_id.clone(),
                JobState::Failed,
                created_at,
                as_of,
                Some(error.to_string()),
            ) {
                fallback = record;
            }
        }
        Ok(Err(_)) | Err(_) => {}
    }

    let current = jobs.get(&job_id).map_err(CoverageRequestError::from)?;
    Ok(EnsureOutcome::Accepted(current.unwrap_or(fallback)))
}

/// Return the stable identifier for a logically equivalent ensure request.
///
/// The payload is the canonical JSON of the request with sorted keys, no
/// spaces, and Python's `ensure_ascii` escaping, hashed with SHA-256 and
/// prefixed `ensure-`.
pub fn ensure_job_id(spec: &EnsureSpec) -> Result<String, CoverageRequestError> {
    spec.validate_interval()?;

    #[derive(Serialize)]
    struct Payload<'a> {
        asset_class: &'a str,
        data_type: &'a str,
        end_date: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        interval: Option<&'a str>,
        start_date: String,
        symbol: String,
    }

    let payload = Payload {
        asset_class: spec.asset_class.value(),
        data_type: spec.data_type.value(),
        end_date: spec.end_date.isoformat(),
        interval: spec.interval.as_deref(),
        start_date: spec.start_date.isoformat(),
        symbol: spec.symbol.trim().to_uppercase(),
    };
    let encoded = serde_json::to_string(&payload).map_err(|error| {
        CoverageRequestError::new(
            "internal_error",
            format!("ensure job id encoding failed: {error}"),
            None,
        )
    })?;
    let digest = Sha256::digest(python_ascii_json(&encoded).as_bytes());
    Ok(format!("ensure-{}", hex::encode(digest)))
}

/// Clamp a caller-supplied deadline to a usable, non-negative duration.
fn normalized_deadline(seconds: f64) -> f64 {
    if seconds.is_finite() && seconds > 0.0 {
        seconds
    } else {
        0.0
    }
}

/// Escape non-ASCII characters exactly like Python's `json.dumps(ensure_ascii=True)`.
///
/// `serde_json` already escapes control characters and quotes; the only
/// difference for `ensure_job_id` payloads is that Python renders every
/// non-ASCII character as one or two lowercase `\uXXXX` escapes.
fn python_ascii_json(text: &str) -> String {
    if text.is_ascii() {
        return text.to_string();
    }
    use std::fmt::Write as _;

    let mut escaped = String::with_capacity(text.len());
    let mut units = [0u16; 2];
    for character in text.chars() {
        if (character as u32) < 0x80 {
            escaped.push(character);
        } else {
            for unit in character.encode_utf16(&mut units) {
                let _ = write!(escaped, "\\u{unit:04x}");
            }
        }
    }
    escaped
}
