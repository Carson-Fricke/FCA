//! On-demand market-lake ensure for the local harness data source.
//!
//! A local backtest reads a materialized Parquet lake, but it must never fail
//! merely because a requested symbol was not fetched before. This client asks
//! the published market-lake ensure contract to fetch the configured symbols
//! over the run window, then polls coverage until every expected session is
//! terminal (complete, empty, delisted, or a truncated partial). The
//! market-lake process owns the provider credentials and the fetch; the
//! harness only issues the request.
//!
//! One request covers the whole window: the lake plans, resumes, and retries
//! its own fetches server-side, so callers never split ranges and a repeat
//! request picks up where the previous run left off. Polling has no overall
//! deadline — the ensure job runs to completion in the lake — and uses a
//! backoff cadence. A closed session the lake left `partial`, or an errored
//! fetch, is a gap the lake treats as actionable and repairs inside the
//! running job, so polling continues while the job is in flight. Only a
//! settled job turns a still-short window into a failure, and the harness then
//! names the sessions the lake could not complete. A session that is still
//! trading is accepted as partial.

use std::thread::sleep;
use std::time::Duration;

use serde_json::{json, Value};

/// Default loopback port of the local market-lake API in the dev stack.
pub const DEFAULT_LAKE_BASE_URL: &str = "http://127.0.0.1:8002";
/// Wall-clock bound for one ensure, job, or coverage HTTP call.
const HTTP_TIMEOUT_SECONDS: u64 = 60;
/// First coverage-poll interval.
const POLL_START: Duration = Duration::from_millis(1_000);
/// Coverage polls never wait longer than this between attempts.
const POLL_MAX: Duration = Duration::from_millis(5_000);

/// The coverage-poll interval that follows one wait.
fn next_poll_interval(current: Duration) -> Duration {
    current.checked_mul(2).unwrap_or(POLL_MAX).min(POLL_MAX)
}

/// Whether one coverage entry is satisfied.
///
/// A session is satisfied when it is fully fetched, has no data, is delisted,
/// or is still trading: an open session is necessarily incomplete while it
/// trades, and waiting cannot complete it.
fn coverage_entry_is_satisfied(entry: &Value) -> bool {
    let status = entry
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or_default();
    match status {
        "complete" | "empty" | "delisted" => true,
        "partial" => entry
            .get("is_partial")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        _ => false,
    }
}

/// Whether every entry in a coverage response is satisfied.
fn coverage_is_satisfied(body: &Value) -> bool {
    let Some(entries) = body.get("entries").and_then(Value::as_array) else {
        return false;
    };
    !entries.is_empty() && entries.iter().all(coverage_entry_is_satisfied)
}

/// Return a message naming every session the lake could not complete.
///
/// This is the diagnostic for a window that is still short once the ensure job
/// has settled. While the job is in flight the same rows are repairable gaps
/// the lake is already re-fetching, so they are not failures yet.
fn coverage_incompleteness(body: &Value) -> Option<String> {
    let entries = body.get("entries").and_then(Value::as_array)?;
    let failures: Vec<String> = entries
        .iter()
        .filter_map(|entry| {
            let status = entry
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let session_open = entry
                .get("is_partial")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let reason = match status {
                "error" => Some(
                    entry
                        .get("error_message")
                        .and_then(Value::as_str)
                        .unwrap_or("the provider fetch failed")
                        .to_string(),
                ),
                "partial" if !session_open => {
                    Some("the closed session was left partial".to_string())
                }
                _ => None,
            }?;
            let symbol = entry.get("symbol").and_then(Value::as_str).unwrap_or("?");
            let data_type = entry
                .get("data_type")
                .and_then(Value::as_str)
                .unwrap_or("?");
            let session = entry
                .get("session_date")
                .and_then(Value::as_str)
                .unwrap_or("?");
            Some(format!("{symbol} {data_type} {session}: {reason}"))
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
enum EnsureJobProgress {
    /// The lake is still working: a repairable gap may still heal.
    InFlight,
    /// The lake finished its plan, or no job was launched: nothing more is
    /// coming for this request.
    Settled,
    /// The lake reported the job failed.
    Failed(String),
}

/// Classify one ensure-job status body.
///
/// A closed session left `partial`, or an errored fetch, is a gap the lake
/// classifies as actionable and repairs inside the running job. Only a settled
/// job turns a still-short window into a failure, so the repair lands instead
/// of being aborted mid-flight.
fn ensure_job_progress(body: &Value) -> EnsureJobProgress {
    match body.get("state").and_then(Value::as_str) {
        Some("failed") => EnsureJobProgress::Failed(
            body.get("error_message")
                .and_then(Value::as_str)
                .unwrap_or("the ensure job failed")
                .to_string(),
        ),
        Some("complete") => EnsureJobProgress::Settled,
        _ => EnsureJobProgress::InFlight,
    }
}

/// The poll loop's verdict for one observation of coverage and job progress.
#[derive(Debug, PartialEq, Eq)]
enum CoverageVerdict {
    /// Every session is satisfied: this ensure is done.
    Satisfied,
    /// Coverage is short but the lake is still repairing it: keep polling.
    Repairing,
    /// Coverage is short and nothing more is coming: this ensure failed.
    Incomplete(String),
}

/// Decide what one coverage poll means.
fn coverage_verdict(coverage: &Value, progress: &EnsureJobProgress) -> CoverageVerdict {
    if coverage_is_satisfied(coverage) {
        return CoverageVerdict::Satisfied;
    }
    match progress {
        EnsureJobProgress::InFlight => CoverageVerdict::Repairing,
        EnsureJobProgress::Failed(message) => {
            CoverageVerdict::Incomplete(format!("market-lake ensure job failed: {message}"))
        }
        EnsureJobProgress::Settled => CoverageVerdict::Incomplete(format!(
            "market-lake could not complete the period: {}",
            coverage_incompleteness(coverage).unwrap_or_else(|| {
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
fn earliest_complete_date(body: &Value) -> Option<String> {
    body.get("entries")
        .and_then(Value::as_array)?
        .iter()
        .filter_map(|entry| {
            if entry.get("status").and_then(Value::as_str) != Some("complete") {
                return None;
            }
            entry
                .get("session_date")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .min()
}

/// The market-lake ensure client used before a local lake read.
#[derive(Debug, Clone)]
pub struct LakeEnsureClient {
    base_url: String,
    token: String,
    timeout: Duration,
}

impl LakeEnsureClient {
    /// Build a client from `LAKE_API_URL`/`LAKE_API_BASE_URL` and the bearer
    /// token. Returns `None` when no token is configured, so an environment
    /// without a running lake keeps its existing behavior.
    #[must_use]
    pub fn from_env() -> Option<Self> {
        let token = std::env::var("LAKE_API__BEARER_TOKEN")
            .ok()
            .filter(|value| !value.trim().is_empty())?;
        let base_url = std::env::var("LAKE_API_URL")
            .or_else(|_| std::env::var("LAKE_API_BASE_URL"))
            .ok()
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_LAKE_BASE_URL.to_string());
        Some(Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            token,
            timeout: Duration::from_secs(HTTP_TIMEOUT_SECONDS),
        })
    }

    /// Ensure one symbol/data-type window is terminally covered.
    ///
    /// The window may span the provider's full history: the lake plans and
    /// resumes its own fetches, so the request is never split and a repeat
    /// request continues an interrupted ensure.
    ///
    /// # Errors
    ///
    /// Returns a message when the lake is unreachable, rejects the request, or
    /// its ensure job settles with coverage still incomplete. Polling has no
    /// overall deadline; only a settled job reports a failure.
    pub fn ensure(
        &self,
        asset_class: &str,
        data_type: &str,
        symbol: &str,
        start_date: &str,
        end_date: &str,
    ) -> Result<(), String> {
        let body = json!({
            "asset_class": asset_class,
            "data_type": data_type,
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
        });
        let response = self
            .post("/v1/catalog/ensure", &body)
            .map_err(|error| format!("market-lake ensure request failed: {error}"))?;
        if coverage_is_satisfied(&response) {
            return Ok(());
        }
        let job_id = response
            .get("job_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        self.poll_coverage(asset_class, data_type, symbol, start_date, end_date, job_id)
            .map_err(|error| {
                format!(
                    "market-lake could not fetch {symbol} {asset_class}/{data_type} \
                 for {start_date}..{end_date}: {error}"
                )
            })
    }

    /// Return the earliest fully covered session date for one symbol window.
    ///
    /// # Errors
    ///
    /// Returns a message when the coverage query fails. A window with no
    /// complete session resolves to `None`.
    pub fn earliest_complete_session(
        &self,
        asset_class: &str,
        data_type: &str,
        symbol: &str,
        start_date: &str,
        end_date: &str,
    ) -> Result<Option<String>, String> {
        let coverage = self
            .get_coverage(asset_class, data_type, symbol, start_date, end_date)
            .map_err(|error| format!("market-lake coverage query failed: {error}"))?;
        Ok(earliest_complete_date(&coverage))
    }

    /// Poll one ensure request until it is satisfied or settled.
    ///
    /// A pending job means the lake is still repairing whatever the coverage
    /// window is short of, so the loop keeps polling. Only a settled job, or a
    /// request that launched none, turns a short window into a failure — which
    /// is what lets a closed session the lake left `partial` be repaired by
    /// the very ensure that would previously have aborted before the repair
    /// landed.
    fn poll_coverage(
        &self,
        asset_class: &str,
        data_type: &str,
        symbol: &str,
        start_date: &str,
        end_date: &str,
        job_id: Option<String>,
    ) -> Result<(), String> {
        let mut interval = POLL_START;
        loop {
            let progress = match &job_id {
                Some(job_id) => ensure_job_progress(&self.get(&format!("/v1/jobs/{job_id}"))?),
                None => EnsureJobProgress::Settled,
            };
            let coverage = self
                .get_coverage(asset_class, data_type, symbol, start_date, end_date)
                .map_err(|error| format!("market-lake coverage query failed: {error}"))?;
            match coverage_verdict(&coverage, &progress) {
                CoverageVerdict::Satisfied => return Ok(()),
                CoverageVerdict::Incomplete(message) => return Err(message),
                CoverageVerdict::Repairing => {}
            }
            sleep(interval);
            interval = next_poll_interval(interval);
        }
    }

    fn get_coverage(
        &self,
        asset_class: &str,
        data_type: &str,
        symbol: &str,
        start_date: &str,
        end_date: &str,
    ) -> Result<Value, String> {
        let path = format!(
            "/v1/catalog/coverage?asset_class={asset_class}&data_type={data_type}\
             &symbol={symbol}&start_date={start_date}&end_date={end_date}"
        );
        self.get(&path)
    }

    fn get(&self, path: &str) -> Result<Value, String> {
        let url = format!("{}{path}", self.base_url);
        let request = ureq::get(&url)
            .timeout(self.timeout)
            .set("Authorization", &format!("Bearer {}", self.token));
        match request.call() {
            Ok(response) => response
                .into_json::<Value>()
                .map_err(|error| error.to_string()),
            Err(ureq::Error::Status(code, response)) => Err(format!(
                "HTTP {code}: {}",
                response.into_string().unwrap_or_default()
            )),
            Err(error) => Err(error.to_string()),
        }
    }

    fn post(&self, path: &str, body: &Value) -> Result<Value, String> {
        let url = format!("{}{path}", self.base_url);
        let request = ureq::post(&url)
            .timeout(self.timeout)
            .set("Authorization", &format!("Bearer {}", self.token));
        match request.send_json(body.clone()) {
            Ok(response) => response
                .into_json::<Value>()
                .map_err(|error| error.to_string()),
            Err(ureq::Error::Status(code, response)) => Err(format!(
                "HTTP {code}: {}",
                response.into_string().unwrap_or_default()
            )),
            Err(error) => Err(error.to_string()),
        }
    }
}

#[cfg(test)]
#[path = "../tests/unit/lake_ensure.rs"]
mod tests;
