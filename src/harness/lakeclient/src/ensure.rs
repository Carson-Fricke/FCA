//! The ensure poll loop's classification rules.
//!
//! These are pure functions over typed coverage and job views, so the poll
//! semantics stay behaviorally identical to the historical `LakeEnsureClient`:
//! one request covers the whole window, polling has no overall deadline, a
//! pending job keeps a short coverage window alive (the lake repairs closed
//! partials and errored fetches inside the running job), and only a settled
//! job, or a request that launched none, turns a still-short window into a
//! failure.

use std::time::Duration;

use crate::models::CoverageEntry;
use crate::models::JobStatus;

/// First coverage-poll interval.
pub(crate) const POLL_START: Duration = Duration::from_millis(100);
/// Coverage polls never wait longer than this between attempts.
pub(crate) const POLL_MAX: Duration = Duration::from_millis(500);

/// The coverage-poll interval that follows one wait.
pub(crate) fn next_poll_interval(current: Duration) -> Duration {
    current.checked_mul(2).unwrap_or(POLL_MAX).min(POLL_MAX)
}

/// Whether one coverage entry is satisfied.
///
/// A session is satisfied when it is fully fetched, has no data, is delisted,
/// or is still trading: an open session is necessarily incomplete while it
/// trades, and waiting cannot complete it.
pub(crate) fn coverage_entry_is_satisfied(entry: &CoverageEntry) -> bool {
    match entry.status.as_str() {
        "complete" | "empty" | "delisted" => true,
        "partial" => entry.is_partial,
        _ => false,
    }
}

/// Whether every entry in a coverage response is satisfied.
pub(crate) fn coverage_entries_satisfied(entries: &[CoverageEntry]) -> bool {
    !entries.is_empty() && entries.iter().all(coverage_entry_is_satisfied)
}

/// Return a message naming every session the lake could not complete.
///
/// This is the diagnostic for a window that is still short once the ensure job
/// has settled. While the job is in flight the same rows are repairable gaps
/// the lake is already re-fetching, so they are not failures yet.
pub(crate) fn coverage_incompleteness(entries: &[CoverageEntry]) -> Option<String> {
    let failures: Vec<String> = entries
        .iter()
        .filter_map(|entry| {
            let reason = match entry.status.as_str() {
                "error" => Some(
                    entry
                        .error_message
                        .clone()
                        .unwrap_or_else(|| "the provider fetch failed".to_string()),
                ),
                "partial" if !entry.is_partial => {
                    Some("the closed session was left partial".to_string())
                }
                _ => None,
            }?;
            let symbol = entry.symbol.as_deref().unwrap_or("?");
            let data_type = entry.data_type.as_deref().unwrap_or("?");
            Some(format!(
                "{symbol} {data_type} {}: {reason}",
                entry.session_date
            ))
        })
        .collect();
    if failures.is_empty() {
        None
    } else {
        Some(failures.join("; "))
    }
}

/// What the lake reports about one ensure job.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum EnsureJobProgress {
    /// The lake is still working: a repairable gap may still heal.
    InFlight,
    /// The lake finished its plan, or no job was launched: nothing more is
    /// coming for this request.
    Settled,
    /// The lake reported the job failed.
    Failed(String),
}

/// Classify one ensure-job status.
///
/// A closed session left `partial`, or an errored fetch, is a gap the lake
/// classifies as actionable and repairs inside the running job. Only a settled
/// job turns a still-short window into a failure, so the repair lands instead
/// of being aborted mid-flight.
pub(crate) fn ensure_job_progress(status: &JobStatus) -> EnsureJobProgress {
    match status.state.as_str() {
        "failed" => EnsureJobProgress::Failed(
            status
                .error_message
                .clone()
                .unwrap_or_else(|| "the ensure job failed".to_string()),
        ),
        "complete" => EnsureJobProgress::Settled,
        _ => EnsureJobProgress::InFlight,
    }
}

/// The poll loop's verdict for one observation of coverage and job progress.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum CoverageVerdict {
    /// Every session is satisfied: this ensure is done.
    Satisfied,
    /// Coverage is short but the lake is still repairing it: keep polling.
    Repairing,
    /// Coverage is short and nothing more is coming: this ensure failed.
    Incomplete(String),
}

/// Decide what one coverage poll means.
pub(crate) fn coverage_verdict(
    entries: &[CoverageEntry],
    progress: &EnsureJobProgress,
) -> CoverageVerdict {
    if coverage_entries_satisfied(entries) {
        return CoverageVerdict::Satisfied;
    }
    match progress {
        EnsureJobProgress::InFlight => CoverageVerdict::Repairing,
        EnsureJobProgress::Failed(message) => {
            CoverageVerdict::Incomplete(format!("market-lake ensure job failed: {message}"))
        }
        EnsureJobProgress::Settled => CoverageVerdict::Incomplete(format!(
            "market-lake could not complete the period: {}",
            coverage_incompleteness(entries).unwrap_or_else(|| {
                "the lake finished its ensure with coverage still incomplete".to_string()
            })
        )),
    }
}

/// Return the earliest fully covered session date in a coverage response.
///
/// A session qualifies only when its fetch is `complete`: `empty` and
/// `delisted` sessions carry no data, and `missing` sessions were never
/// fetched. ISO dates compare lexicographically, so a linear minimum is the
/// chronological earliest.
pub(crate) fn earliest_complete_date(entries: &[CoverageEntry]) -> Option<String> {
    entries
        .iter()
        .filter(|entry| entry.status == "complete")
        .map(|entry| entry.session_date.clone())
        .min()
}
