//! The market-lake HTTP client.

use arrow_buffer::Buffer;
use arrow_ipc::reader::StreamDecoder;
use harness_contracts::progress::{ExecutionProgress, ExecutionStage, ProgressReporter};
use std::collections::BTreeMap;
use std::fmt;
use std::time::{Duration, Instant};

use arrow_array::RecordBatch;
use futures_util::stream::{self, StreamExt};
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION};
use serde_json::Value;

use crate::ensure::{
    coverage_entries_satisfied, coverage_entry_is_satisfied, coverage_verdict,
    earliest_complete_date, ensure_job_progress, next_poll_interval, CoverageVerdict,
    EnsureJobProgress, POLL_START,
};
use crate::error::{EnsureFailure, LakeClientError};
use crate::models::{CoverageReport, EnsureResponse, FactsResponse, JobStatus, UniverseResponse};
use crate::spec::{EnsureSpec, ReadSpec};

/// Default Azure market-lake endpoint; environment overrides can select a local lake.
pub const DEFAULT_LAKE_BASE_URL: &str =
    "https://fca-ml-dev-api.lemonhill-3b437822.westus2.azurecontainerapps.io";
/// Wall-clock bound for control calls; connect/read inactivity bound for Arrow streams.
const HTTP_TIMEOUT_SECONDS: u64 = 60;
/// The most ensure or read requests the batch helpers keep in flight at once.
///
/// The lake's shared per-key provider lane is the real rate limit, so this
/// bound keeps the fetch budget saturated without opening one socket per
/// configured symbol when a universe grows.
pub const MAX_PARALLEL_FETCHES: usize = 8;

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
    /// `LAKE_API_URL`, then `LAKE_API_BASE_URL`, then the Azure default.
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
        let base_url = ["LAKE_API_URL", "LAKE_API_BASE_URL"]
            .into_iter()
            .filter_map(|name| std::env::var(name).ok())
            .map(|value| value.trim().to_string())
            .find(|value| !value.is_empty())
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
    stream_http: reqwest::Client,
    reporter: Option<ProgressReporter>,
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
            .default_headers(headers.clone())
            .build()
            .map_err(|error| {
                LakeClientError::Config(format!(
                    "the market-lake HTTP client could not be built: {error}"
                ))
            })?;
        let stream_http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(HTTP_TIMEOUT_SECONDS))
            .read_timeout(Duration::from_secs(HTTP_TIMEOUT_SECONDS))
            .default_headers(headers)
            .build()
            .map_err(|error| LakeClientError::Config(error.to_string()))?;
        Ok(Self {
            config,
            http,
            stream_http,
            reporter: None,
        })
    }

    /// Attach the execution owner's progress port; clones share this port.
    pub fn with_progress(mut self, reporter: ProgressReporter) -> Self {
        self.reporter = Some(reporter);
        self
    }

    /// Publish an observed execution stage without taking ownership of its job.
    pub fn report_progress(&self, progress: ExecutionProgress) -> Result<(), LakeClientError> {
        if let Some(reporter) = &self.reporter {
            reporter(progress).map_err(LakeClientError::Protocol)?;
        }
        Ok(())
    }

    fn report_coverage(
        &self,
        spec: &EnsureSpec,
        entries: &[crate::models::CoverageEntry],
        cached: u64,
    ) -> Result<(), LakeClientError> {
        let mut progress = ExecutionProgress::stage(ExecutionStage::PreparingData);
        progress.symbol = Some(spec.symbol.clone());
        progress.data_type = Some(spec.data_type.clone());
        progress.start_date = Some(spec.start_date.clone());
        progress.end_date = Some(spec.end_date.clone());
        progress.sessions_total = Some(entries.len() as u64);
        progress.sessions_ready = Some(
            entries
                .iter()
                .filter(|entry| coverage_entry_is_satisfied(entry))
                .count() as u64,
        );
        progress.sessions_cached = Some(cached);
        progress.rows = entries.iter().filter_map(|entry| entry.row_count).sum();
        progress.completed = coverage_entries_satisfied(entries);
        self.report_progress(progress)
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
        let cached = if self.reporter.is_some() {
            let report = self.coverage(spec).await?;
            let cached = report
                .entries
                .iter()
                .filter(|entry| coverage_entry_is_satisfied(entry))
                .count() as u64;
            self.report_coverage(spec, &report.entries, cached)?;
            cached
        } else {
            0
        };
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
        if self.reporter.is_some() && !entries.is_empty() {
            self.report_coverage(spec, &entries, cached)?;
        }
        if coverage_entries_satisfied(&entries) {
            return Ok(());
        }
        self.poll_coverage(spec, response.job_id, cached)
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
        let mut batches = Vec::new();
        self.visit_arrow(spec, |batch| {
            batches.push(batch);
            Ok(())
        })
        .await?;
        Ok(batches)
    }

    /// Decode and consume Arrow batches as they arrive. The 60-second limit is
    /// an idle-read timeout, so a healthy large stream can take longer overall.
    /// No whole-response byte buffer or automatic replay is used.
    pub async fn visit_arrow<F>(
        &self,
        spec: &ReadSpec,
        mut consume: F,
    ) -> Result<(), LakeClientError>
    where
        F: FnMut(RecordBatch) -> Result<(), LakeClientError> + Send,
    {
        let mut progress = ExecutionProgress::stage(ExecutionStage::ReadingData);
        progress.symbol = Some(spec.symbol.clone());
        progress.data_type = Some(spec.data_type.clone());
        progress.start_date = Some(spec.start_date.clone());
        progress.end_date = Some(spec.end_date.clone());
        progress.request_timeout_seconds = Some(HTTP_TIMEOUT_SECONDS);
        self.report_progress(progress.clone())?;
        let mut response = self
            .stream_http
            .get(self.url("/v1/read"))
            .query(&spec.query_params())
            .send()
            .await
            .map_err(|error| read_transport_error(spec, &error, 0))?;
        let status = response.status();
        if status.as_u16() == 404 {
            let bytes = response
                .bytes()
                .await
                .map_err(|error| read_transport_error(spec, &error, 0))?;
            let error = http_error(404, &bytes);
            if matches!(&error, LakeClientError::Http { code: Some(code), .. } if code == "no_data")
            {
                progress.completed = true;
                self.report_progress(progress)?;
                return Ok(());
            }
            return Err(error);
        }
        if !status.is_success() {
            let bytes = response
                .bytes()
                .await
                .map_err(|error| read_transport_error(spec, &error, 0))?;
            return Err(http_error(status.as_u16(), &bytes));
        }
        let mut decoder = StreamDecoder::new();
        let mut last_report = Instant::now();
        while let Some(chunk) = match response.chunk().await {
            Ok(chunk) => chunk,
            Err(error) => {
                self.report_progress(progress.clone())?;
                return Err(read_transport_error(spec, &error, progress.bytes));
            }
        } {
            progress.bytes += chunk.len() as u64;
            let mut buffer = Buffer::from(chunk.as_ref());
            while !buffer.is_empty() {
                let decoded = decoder
                    .decode(&mut buffer)
                    .map_err(|error| LakeClientError::Decode(error.to_string()));
                if decoded.is_err() {
                    self.report_progress(progress.clone())?;
                }
                if let Some(batch) = decoded? {
                    progress.rows += batch.num_rows() as u64;
                    consume(batch)?;
                }
            }
            if last_report.elapsed() >= Duration::from_secs(1) {
                self.report_progress(progress.clone())?;
                last_report = Instant::now();
            }
        }
        if let Err(error) = decoder.finish() {
            self.report_progress(progress.clone())?;
            return Err(LakeClientError::Decode(error.to_string()));
        }
        progress.completed = true;
        self.report_progress(progress)?;
        Ok(())
    }

    /// Ensure every spec concurrently, preserving the input order.
    ///
    /// Up to [`MAX_PARALLEL_FETCHES`] ensures are in flight at once and every
    /// spec is attempted even when another fails, so the lake runs its fetches
    /// concurrently instead of one symbol at a time. Results keep the spec
    /// order, letting a caller report the first failing symbol deterministically.
    pub async fn ensure_many(&self, specs: &[EnsureSpec]) -> Vec<Result<(), LakeClientError>> {
        let requests = specs.iter().cloned().map(|spec| {
            let client = self.clone();
            async move { client.ensure(&spec).await }
        });
        stream::iter(requests)
            .buffered(MAX_PARALLEL_FETCHES)
            .collect()
            .await
    }

    /// Read every spec concurrently, preserving the input order.
    ///
    /// Same bounded fan-out and ordering contract as [`Self::ensure_many`].
    pub async fn read_many(
        &self,
        specs: &[ReadSpec],
    ) -> Vec<Result<Vec<RecordBatch>, LakeClientError>> {
        let requests = specs.iter().cloned().map(|spec| {
            let client = self.clone();
            async move { client.read_arrow(&spec).await }
        });
        stream::iter(requests)
            .buffered(MAX_PARALLEL_FETCHES)
            .collect()
            .await
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
        cached: u64,
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
            if self.reporter.is_some() {
                self.report_coverage(spec, &report.entries, cached)?;
            }
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

    /// Resolve one option with explicit point-in-time selection evidence.
    pub async fn resolve_option(
        &self,
        selector: &harness_contracts::OptionSelector,
    ) -> Result<harness_contracts::ResolvedOption, LakeClientError> {
        selector
            .validate()
            .map_err(|e| LakeClientError::Config(e.to_string()))?;
        let value = self
            .post_value(
                "/v1/options/resolve",
                &serde_json::to_value(selector)
                    .map_err(|e| LakeClientError::Config(e.to_string()))?,
            )
            .await?;
        let resolved: harness_contracts::ResolvedOption = serde_json::from_value(value)
            .map_err(|e| LakeClientError::Protocol(format!("invalid option resolution: {e}")))?;
        resolved
            .validate_against(selector)
            .map_err(|e| LakeClientError::Protocol(e.to_string()))?;
        Ok(resolved)
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

fn read_transport_error(spec: &ReadSpec, error: &reqwest::Error, bytes: u64) -> LakeClientError {
    let cause = if error.is_timeout() {
        if error.is_connect() {
            "connection timed out after 60 seconds"
        } else {
            "idle read timed out after 60 seconds"
        }
        .to_string()
    } else {
        let mut detail = error.to_string();
        let mut source = std::error::Error::source(error);
        while let Some(cause) = source {
            detail.push_str(&format!(": {cause}"));
            source = cause.source();
        }
        detail
    };
    LakeClientError::Transport(format!(
        "reading {} {} {} through {} after {bytes} bytes: {cause}",
        spec.symbol, spec.data_type, spec.start_date, spec.end_date
    ))
}
