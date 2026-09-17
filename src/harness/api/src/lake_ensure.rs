//! On-demand market-lake ensure for the local harness data source.
//!
//! A local backtest reads a materialized Parquet lake, but it must never fail
//! merely because a requested symbol was not fetched before. This client asks
//! the published market-lake ensure contract to fetch the configured symbols
//! over the run window, then polls coverage until every expected session is
//! terminal (complete, empty, or delisted). The market-lake process owns the
//! provider credentials and the fetch; the harness only issues the request.

use std::thread::sleep;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

/// Default loopback port of the local market-lake API in the dev stack.
pub const DEFAULT_LAKE_BASE_URL: &str = "http://127.0.0.1:8002";
/// Default wall-clock bound for one ensure request and its coverage polling.
pub const DEFAULT_ENSURE_TIMEOUT_SECONDS: u64 = 1_800;
const DEFAULT_POLL_MILLIS: u64 = 1_000;

/// A terminal coverage status for one expected session.
fn is_terminal_status(status: &str) -> bool {
    matches!(status, "complete" | "empty" | "delisted")
}

/// Whether every entry in a coverage response is terminal.
fn coverage_is_terminal(body: &Value) -> bool {
    let Some(entries) = body.get("entries").and_then(Value::as_array) else {
        return false;
    };
    !entries.is_empty()
        && entries.iter().all(|entry| {
            entry
                .get("status")
                .and_then(Value::as_str)
                .is_some_and(is_terminal_status)
        })
}

/// The market-lake ensure client used before a local lake read.
#[derive(Debug, Clone)]
pub struct LakeEnsureClient {
    base_url: String,
    token: String,
    timeout: Duration,
    poll: Duration,
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
        let timeout_seconds = std::env::var("LAKE_ENSURE_TIMEOUT_SECONDS")
            .ok()
            .and_then(|value| value.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_ENSURE_TIMEOUT_SECONDS)
            .max(1);
        Some(Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            token,
            timeout: Duration::from_secs(timeout_seconds),
            poll: Duration::from_millis(DEFAULT_POLL_MILLIS),
        })
    }

    /// Ensure one symbol/data-type window is terminally covered.
    ///
    /// # Errors
    ///
    /// Returns a message when the lake is unreachable, rejects the request, the
    /// ensure job fails, or coverage is still not terminal before the timeout.
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
        if coverage_is_terminal(&response) {
            return Ok(());
        }
        let job_id = response
            .get("job_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        self.poll_coverage(
            asset_class, data_type, symbol, start_date, end_date, job_id,
        )
        .map_err(|error| {
            format!(
                "market-lake could not fetch {symbol} {asset_class}/{data_type} \
                 for {start_date}..{end_date}: {error}"
            )
        })
    }

    fn poll_coverage(
        &self,
        asset_class: &str,
        data_type: &str,
        symbol: &str,
        start_date: &str,
        end_date: &str,
        job_id: Option<String>,
    ) -> Result<(), String> {
        let deadline = Instant::now() + self.timeout;
        loop {
            if let Some(job_id) = &job_id {
                if let Some(failure) = self.job_failure(job_id)? {
                    return Err(failure);
                }
            }
            let coverage = self
                .get_coverage(asset_class, data_type, symbol, start_date, end_date)
                .map_err(|error| format!("market-lake coverage query failed: {error}"))?;
            if coverage_is_terminal(&coverage) {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(format!(
                    "timed out after {}s waiting for coverage",
                    self.timeout.as_secs()
                ));
            }
            sleep(self.poll);
        }
    }

    fn job_failure(&self, job_id: &str) -> Result<Option<String>, String> {
        let path = format!("/v1/jobs/{job_id}");
        let body = self.get(&path)?;
        if body.get("state").and_then(Value::as_str) == Some("failed") {
            let message = body
                .get("error_message")
                .and_then(Value::as_str)
                .unwrap_or("the ensure job failed");
            return Ok(Some(message.to_string()));
        }
        Ok(None)
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
