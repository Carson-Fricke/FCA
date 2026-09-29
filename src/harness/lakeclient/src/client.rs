//! The market-lake HTTP client.

use std::collections::BTreeMap;
use std::fmt;
use std::time::Duration;

use arrow_array::RecordBatch;
use arrow_ipc::reader::StreamReader;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION};
use serde_json::Value;

use crate::ensure::{
    coverage_entries_satisfied, coverage_verdict, earliest_complete_date, ensure_job_progress,
    next_poll_interval, CoverageVerdict, EnsureJobProgress, POLL_START,
};
use crate::error::{EnsureFailure, LakeClientError};
use crate::models::{CoverageReport, EnsureResponse, FactsResponse, JobStatus, UniverseResponse};
use crate::spec::{EnsureSpec, ReadSpec};

/// Default loopback port of the local market-lake API in the dev stack.
pub const DEFAULT_LAKE_BASE_URL: &str = "http://127.0.0.1:8002";
/// Wall-clock bound for one ensure, job, coverage, or read HTTP call.
const HTTP_TIMEOUT_SECONDS: u64 = 60;

/// Resolved market-lake connection settings.
///
/// The debug rendering redacts the bearer token so it can never leak through a
/// log line; the token is sent only in the `Authorization` header.
#[derive(Clone, PartialEq, Eq)]
pub struct LakeClientConfig {
    base_url: String,
    bearer_token: String,
}

impl fmt::Debug for LakeClientConfig {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LakeClientConfig")
            .field("base_url", &self.base_url)
            .field("bearer_token", &"<redacted>")
            .finish()
    }
}

impl LakeClientConfig {
    /// Build a configuration from an explicit base URL and bearer token.
    ///
    /// # Errors
    ///
    /// Returns [`LakeClientError::Config`] when either value is empty.
    pub fn new(
        base_url: impl Into<String>,
        bearer_token: impl Into<String>,
    ) -> Result<Self, LakeClientError> {
        let base_url = base_url.into().trim().trim_end_matches('/').to_string();
        if base_url.is_empty() {
            return Err(LakeClientError::Config(
                "the market-lake base URL must not be empty".to_string(),
            ));
        }
        let bearer_token = bearer_token.into().trim().to_string();
        if bearer_token.is_empty() {
            return Err(LakeClientError::Config(
                "the market-lake bearer token must not be empty".to_string(),
            ));
        }
        Ok(Self {
            base_url,
            bearer_token,
        })
    }

    /// Resolve the configuration from the environment.
    ///
    /// `LAKE_API__BEARER_TOKEN` is required; the base URL resolves from
    /// `LAKE_API_URL`, then `LAKE_API_BASE_URL`, then the loopback default.
    ///
    /// # Errors
    ///
    /// Returns [`LakeClientError::Config`] when no bearer token is configured
    /// or a configured value is unusable.
    pub fn from_env() -> Result<Self, LakeClientError> {
        let bearer_token = std::env::var("LAKE_API__BEARER_TOKEN")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                LakeClientError::Config(
                    "LAKE_API__BEARER_TOKEN is not set: the market-lake bearer token is required"
                        .to_string(),
                )
            })?;
        let base_url = std::env::var("LAKE_API_URL")
            .or_else(|_| std::env::var("LAKE_API_BASE_URL"))
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| DEFAULT_LAKE_BASE_URL.to_string());
        Self::new(base_url, bearer_token)
    }

    /// The resolved base URL with any trailing slash removed.
    #[must_use]
    pub fn base_url(&self) -> &str {
        &self.base_url
    }
}

/// An async client over the frozen market-lake routes.
#[derive(Clone)]
pub struct LakeClient {
    config: LakeClientConfig,
    http: reqwest::Client,
}

impl fmt::Debug for LakeClient {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LakeClient")
            .field("base_url", &self.config.base_url)
            .field("bearer_token", &"<redacted>")
            .finish()
    }
}

impl LakeClient {
    /// Build a client from resolved configuration.
    ///
    /// # Errors
    ///
    /// Returns [`LakeClientError::Config`] when the bearer token cannot be
    /// rendered as an HTTP header value or the transport cannot be built.
    pub fn new(config: LakeClientConfig) -> Result<Self, LakeClientError> {
        let mut headers = HeaderMap::new();
        let value =
            HeaderValue::from_str(&format!("Bearer {}", config.bearer_token)).map_err(|_| {
                LakeClientError::Config(
                    "the market-lake bearer token is not a valid HTTP header value".to_string(),
                )
            })?;
        headers.insert(AUTHORIZATION, value);
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(HTTP_TIMEOUT_SECONDS))
            .default_headers(headers)
            .build()
            .map_err(|error| {
                LakeClientError::Config(format!(
                    "the market-lake HTTP client could not be built: {error}"
                ))
            })?;
        Ok(Self { config, http })
    }

    /// Build a client from `LAKE_API_URL`/`LAKE_API_BASE_URL` and
    /// `LAKE_API__BEARER_TOKEN`.
    ///
    /// # Errors
    ///
    /// Returns [`LakeClientError::Config`] when the environment has no bearer
    /// token.
    pub fn from_env() -> Result<Self, LakeClientError> {
        Self::new(LakeClientConfig::from_env()?)
    }

    /// The resolved base URL with any trailing slash removed.
    #[must_use]
    pub fn base_url(&self) -> &str {
        self.config.base_url()
    }

    /// Ensure one symbol/data-type window is terminally covered.
    ///
    /// The window may span the provider's full history: the lake plans and
    /// resumes its own fetches, so the request is never split and a repeat
    /// request continues an interrupted ensure. Polling has no overall
    /// deadline; only a settled job reports a failure.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the lake is unreachable, rejects the
    /// request, or its ensure job settles with coverage still incomplete.
    pub async fn ensure(&self, spec: &EnsureSpec) -> Result<(), LakeClientError> {
        let value = self
            .post_value("/v1/catalog/ensure", &spec.ensure_body())
            .await
            .map_err(|error| LakeClientError::EnsureRequest(error.to_string()))?;
        let response: EnsureResponse = serde_json::from_value(value).map_err(|error| {
            LakeClientError::EnsureRequest(format!(
                "market-lake ensure response is not valid: {error}"
            ))
        })?;
        let entries = response.entries.unwrap_or_default();
        if coverage_entries_satisfied(&entries) {
            return Ok(());
        }
        self.poll_coverage(spec, response.job_id)
            .await
            .map_err(|error| {
                LakeClientError::EnsureFailed(Box::new(EnsureFailure {
                    symbol: spec.symbol.clone(),
                    asset_class: spec.asset_class.clone(),
                    data_type: spec.data_type.clone(),
                    start_date: spec.start_date.clone(),
                    end_date: spec.end_date.clone(),
                    detail: error.to_string(),
                }))
            })
    }

    /// Fetch the coverage report for one window.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the request or decode fails.
    pub async fn coverage(&self, spec: &EnsureSpec) -> Result<CoverageReport, LakeClientError> {
        let value = self
            .get_value("/v1/catalog/coverage", &spec.query_params())
            .await?;
        serde_json::from_value(value).map_err(|error| {
            LakeClientError::Protocol(format!(
                "market-lake coverage response is not valid: {error}"
            ))
        })
    }

    /// Fetch the current state of one ensure job.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the request or decode fails.
    pub async fn job(&self, job_id: &str) -> Result<JobStatus, LakeClientError> {
        let value = self.get_value(&format!("/v1/jobs/{job_id}"), &[]).await?;
        serde_json::from_value(value).map_err(|error| {
            LakeClientError::Protocol(format!("market-lake job response is not valid: {error}"))
        })
    }

    /// Return the earliest fully covered session date for one window.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the coverage query fails. A window with no
    /// complete session resolves to `None`.
    pub async fn earliest_complete_session(
        &self,
        spec: &EnsureSpec,
    ) -> Result<Option<String>, LakeClientError> {
        let report = self
            .coverage(spec)
            .await
            .map_err(|error| LakeClientError::CoverageQuery(error.to_string()))?;
        Ok(earliest_complete_date(&report.entries))
    }

    /// Read the ensured dataset window as decoded Arrow IPC batches.
    ///
    /// Rows arrive in the lake's frozen order (timestamp, then deterministic
    /// ordering keys). A `404 no_data` response maps to an empty batch list:
    /// a window without ensured data is a normal empty read, not an error.
    ///
    /// # Errors
    ///
    /// Returns a typed error for a non-success status other than `no_data`, a
    /// transport failure, or a stream that does not decode as Arrow IPC.
    pub async fn read_arrow(&self, spec: &ReadSpec) -> Result<Vec<RecordBatch>, LakeClientError> {
        let response = self
            .http
            .get(self.url("/v1/read"))
            .query(&spec.query_params())
            .send()
            .await
            .map_err(|error| LakeClientError::Transport(error.to_string()))?;
        let status = response.status();
        let bytes = response
            .bytes()
            .await
            .map_err(|error| LakeClientError::Transport(error.to_string()))?;
        if status.as_u16() == 404 {
            return Ok(Vec::new());
        }
        if !status.is_success() {
            return Err(http_error(status.as_u16(), &bytes));
        }
        decode_arrow_stream(&bytes)
    }

    /// Return the as-of universe symbols.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the request or decode fails.
    pub async fn reference_universe(&self, as_of: &str) -> Result<Vec<String>, LakeClientError> {
        let value = self
            .get_value("/v1/reference/universe", &[("as_of", as_of.to_string())])
            .await?;
        let response: UniverseResponse = serde_json::from_value(value).map_err(|error| {
            LakeClientError::Protocol(format!(
                "market-lake universe response is not valid: {error}"
            ))
        })?;
        Ok(response.symbols)
    }

    /// Return the engine fact-sheet entries per symbol as of `as_of`.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the request or decode fails.
    pub async fn reference_facts(
        &self,
        symbols: &[String],
        as_of: &str,
    ) -> Result<BTreeMap<String, BTreeMap<String, String>>, LakeClientError> {
        let params = vec![("as_of", as_of.to_string()), ("symbols", symbols.join(","))];
        let value = self.get_value("/v1/reference/facts", &params).await?;
        let response: FactsResponse = serde_json::from_value(value).map_err(|error| {
            LakeClientError::Protocol(format!("market-lake facts response is not valid: {error}"))
        })?;
        Ok(response.facts)
    }

    /// Poll one ensure request until it is satisfied or settled.
    async fn poll_coverage(
        &self,
        spec: &EnsureSpec,
        job_id: Option<String>,
    ) -> Result<(), LakeClientError> {
        let mut interval = POLL_START;
        loop {
            let progress = match &job_id {
                Some(job_id) => ensure_job_progress(&self.job(job_id).await?),
                None => EnsureJobProgress::Settled,
            };
            let report = self
                .coverage(spec)
                .await
                .map_err(|error| LakeClientError::CoverageQuery(error.to_string()))?;
            match coverage_verdict(&report.entries, &progress) {
                CoverageVerdict::Satisfied => return Ok(()),
                CoverageVerdict::Incomplete(message) => {
                    return Err(LakeClientError::EnsureIncomplete(message))
                }
                CoverageVerdict::Repairing => {}
            }
            tokio::time::sleep(interval).await;
            interval = next_poll_interval(interval);
        }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.config.base_url)
    }

    async fn get_value(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> Result<Value, LakeClientError> {
        let response = self
            .http
            .get(self.url(path))
            .query(params)
            .send()
            .await
            .map_err(|error| LakeClientError::Transport(error.to_string()))?;
        self.value(response).await
    }

    async fn post_value(&self, path: &str, body: &Value) -> Result<Value, LakeClientError> {
        let response = self
            .http
            .post(self.url(path))
            .json(body)
            .send()
            .await
            .map_err(|error| LakeClientError::Transport(error.to_string()))?;
        self.value(response).await
    }

    async fn value(&self, response: reqwest::Response) -> Result<Value, LakeClientError> {
        let status = response.status();
        let bytes = response
            .bytes()
            .await
            .map_err(|error| LakeClientError::Transport(error.to_string()))?;
        if !status.is_success() {
            return Err(http_error(status.as_u16(), &bytes));
        }
        serde_json::from_slice(&bytes).map_err(|error| {
            LakeClientError::Protocol(format!("market-lake response is not valid JSON: {error}"))
        })
    }
}

/// Decode one Arrow IPC stream into record batches.
fn decode_arrow_stream(bytes: &[u8]) -> Result<Vec<RecordBatch>, LakeClientError> {
    let reader = StreamReader::try_new(std::io::Cursor::new(bytes), None)
        .map_err(|error| LakeClientError::Decode(error.to_string()))?;
    let mut batches = Vec::new();
    for batch in reader {
        batches.push(batch.map_err(|error| LakeClientError::Decode(error.to_string()))?);
    }
    Ok(batches)
}

/// Render one non-success response with the lake's error code when present.
fn http_error(status: u16, body: &[u8]) -> LakeClientError {
    let code = serde_json::from_slice::<Value>(body)
        .ok()
        .and_then(|value| {
            value
                .get("error")
                .and_then(|error| error.get("code"))
                .and_then(Value::as_str)
                .map(str::to_string)
        });
    LakeClientError::Http {
        status,
        code,
        body: String::from_utf8_lossy(body).to_string(),
    }
}
