//! Fair-access HTTP client for reference data providers.
//!
//! Ports `lake.reference.http`. SEC EDGAR requires a descriptive `User-Agent`
//! header and caps programmatic access at 10 requests per second; Nasdaq
//! Trader serves the symbol directory over plain HTTPS. The client owns a
//! rate limiter applied to every request attempt and mirrors the provider
//! retry discipline: exponential backoff with `Retry-After` honored for
//! rate-limit responses. Transport, pacing, and clocks are injectable so
//! tests never touch the network.
//!
//! The SEC requirements are enforced by construction: every attempt passes
//! through [`RateLimiter::acquire`] (defaulting to the 10 requests/second
//! fair-access cap), and every attempt carries the validated `User-Agent`
//! header.

use std::future::Future;
use std::pin::Pin;
use std::sync::OnceLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use lake_contracts::DateTime;
use serde_json::Value;
use tokio::sync::Mutex;

use crate::error::ReferenceError;

/// The SEC fair-access default: no more than 10 requests per second.
pub const DEFAULT_REQUESTS_PER_SECOND: f64 = 10.0;

/// Statuses the retry policy treats as transient.
const RETRYABLE_STATUS_CODES: [u16; 7] = [408, 425, 429, 500, 502, 503, 504];

/// A boxed, sendable future returned by [`Sleeper::sleep`].
pub type BoxSleepFuture<'a> = Pin<Box<dyn Future<Output = ()> + Send + 'a>>;

/// A boxed, sendable future returned by [`ReferenceTransport::request`].
pub type BoxRequestFuture<'a> =
    Pin<Box<dyn Future<Output = Result<TransportResponse, TransportFailure>> + Send + 'a>>;

/// A monotonic float clock used by the rate limiter.
pub trait FloatClock: Send + Sync {
    /// The current monotonic instant in seconds.
    fn now(&self) -> f64;
}

/// An async sleep used by the rate limiter and the retry backoff.
pub trait Sleeper: Send + Sync {
    /// Sleep for `seconds` and complete when the wait elapses.
    fn sleep(&self, seconds: f64) -> BoxSleepFuture<'_>;
}

/// A wall clock returning UTC instants, used to resolve `Retry-After` dates.
pub trait DatetimeClock: Send + Sync {
    /// The current UTC instant.
    fn now(&self) -> DateTime;
}

/// One provider response: status, headers, and the raw body.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TransportResponse {
    /// The HTTP status code.
    pub status: u16,
    /// The response headers in received order.
    pub headers: Vec<(String, String)>,
    /// The raw payload bytes.
    pub content: Vec<u8>,
}

/// A transport-level failure that never produced a usable response.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TransportFailure {
    /// The request could not be sent or completed.
    Transport(String),
    /// The request timed out.
    Timeout(String),
}

impl TransportFailure {
    fn message(self) -> String {
        match self {
            Self::Transport(message) | Self::Timeout(message) => message,
        }
    }
}

/// Request surface implemented by the production reqwest client and by test
/// fakes.
pub trait ReferenceTransport: Send + Sync {
    /// Send one GET request with headers and a whole-request timeout.
    fn request<'a>(
        &'a self,
        url: &'a str,
        headers: &'a [(String, String)],
        timeout_seconds: f64,
    ) -> BoxRequestFuture<'a>;
}

/// Fetch surface consumed by the ingest pipelines.
///
/// Ingest entrypoints only need `get_bytes` and `get_json`, so offline test
/// doubles can stand in for the rate-limited client.
pub trait ReferenceFetchClient: Send + Sync {
    /// Return the raw payload bytes for one URL.
    fn get_bytes<'a>(
        &'a self,
        url: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ReferenceError>> + Send + 'a>>;

    /// Return the decoded JSON document for one URL.
    fn get_json<'a>(
        &'a self,
        url: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Value, ReferenceError>> + Send + 'a>>;
}

/// Production transport over a reqwest client.
///
/// HTTP/1.1 only: HTTP/2 multiplexes every concurrent request through one
/// connection and its flow control collapses fetch bandwidth.
pub struct ReqwestReferenceTransport {
    client: reqwest::Client,
}

impl ReqwestReferenceTransport {
    /// Build a transport with a default whole-request timeout in seconds.
    ///
    /// Non-positive or non-finite timeouts fall back to 30 seconds because
    /// reqwest rejects them outright.
    pub fn new(timeout_seconds: f64) -> Self {
        let timeout = Duration::try_from_secs_f64(timeout_seconds)
            .unwrap_or_else(|_| Duration::from_secs(30));
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .http1_only()
            .build()
            .expect("reqwest client construction");
        Self { client }
    }
}

impl ReferenceTransport for ReqwestReferenceTransport {
    fn request<'a>(
        &'a self,
        url: &'a str,
        headers: &'a [(String, String)],
        timeout_seconds: f64,
    ) -> BoxRequestFuture<'a> {
        Box::pin(async move {
            let timeout = Duration::try_from_secs_f64(timeout_seconds)
                .unwrap_or_else(|_| Duration::from_secs(30));
            let mut request = self.client.get(url).timeout(timeout);
            for (name, value) in headers {
                request = request.header(name.as_str(), value.as_str());
            }
            let response = match request.send().await {
                Ok(response) => response,
                Err(error) => {
                    let message = error.to_string();
                    return Err(if error.is_timeout() {
                        TransportFailure::Timeout(message)
                    } else {
                        TransportFailure::Transport(message)
                    });
                }
            };
            let status = response.status().as_u16();
            let response_headers = response
                .headers()
                .iter()
                .map(|(name, value)| {
                    (
                        name.as_str().to_string(),
                        value.to_str().unwrap_or_default().to_string(),
                    )
                })
                .collect();
            let content = response
                .bytes()
                .await
                .map_err(|error| TransportFailure::Transport(error.to_string()))?
                .to_vec();
            Ok(TransportResponse {
                status,
                headers: response_headers,
                content,
            })
        })
    }
}

/// The process-wide monotonic epoch for the default rate limiter clock.
static MONOTONIC_START: OnceLock<Instant> = OnceLock::new();

/// The default monotonic float clock.
struct MonotonicClock;

impl FloatClock for MonotonicClock {
    fn now(&self) -> f64 {
        MONOTONIC_START
            .get_or_init(std::time::Instant::now)
            .elapsed()
            .as_secs_f64()
    }
}

/// The default async sleeper over tokio time.
struct TokioSleeper;

impl Sleeper for TokioSleeper {
    fn sleep(&self, seconds: f64) -> BoxSleepFuture<'_> {
        Box::pin(async move {
            tokio::time::sleep(Duration::try_from_secs_f64(seconds.max(0.0)).unwrap_or_default())
                .await;
        })
    }
}

/// The default wall clock over the system clock.
struct SystemUtcClock;

impl DatetimeClock for SystemUtcClock {
    fn now(&self) -> DateTime {
        let micros = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|elapsed| elapsed.as_micros() as i64)
            .unwrap_or_default();
        DateTime::from_micros(micros)
    }
}

/// Pace concurrent request attempts at a fixed requests-per-second rate.
///
/// Ports Python's `RateLimiter`: callers hold the pacing slot across the
/// wait, so concurrent callers serialize at one request per minimum interval.
pub struct RateLimiter {
    requests_per_second: f64,
    min_interval: f64,
    clock: Box<dyn FloatClock>,
    sleeper: Box<dyn Sleeper>,
    next_available: Mutex<f64>,
}

impl core::fmt::Debug for RateLimiter {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        formatter
            .debug_struct("RateLimiter")
            .field("requests_per_second", &self.requests_per_second)
            .finish()
    }
}

impl RateLimiter {
    /// Build a limiter at `requests_per_second` with the default tokio clock.
    pub fn new(requests_per_second: f64) -> Result<Self, ReferenceError> {
        Self::with_pacing(
            requests_per_second,
            Box::new(MonotonicClock),
            Box::new(TokioSleeper),
        )
    }

    /// Build a limiter with injectable pacing for tests.
    pub fn with_pacing(
        requests_per_second: f64,
        clock: Box<dyn FloatClock>,
        sleeper: Box<dyn Sleeper>,
    ) -> Result<Self, ReferenceError> {
        if requests_per_second <= 0.0 {
            return Err(ReferenceError::invalid(
                "requests_per_second must be positive",
            ));
        }
        Ok(Self {
            min_interval: 1.0 / requests_per_second,
            requests_per_second,
            clock,
            sleeper,
            next_available: Mutex::new(f64::NEG_INFINITY),
        })
    }

    /// The configured requests-per-second rate.
    pub fn requests_per_second(&self) -> f64 {
        self.requests_per_second
    }

    /// Wait until the next request slot, reserving it for this caller.
    pub async fn acquire(&self) {
        let mut next_available = self.next_available.lock().await;
        let now = self.clock.now();
        let wait = *next_available - now;
        if wait > 0.0 {
            self.sleeper.sleep(wait).await;
        }
        let now = self.clock.now();
        *next_available = now.max(*next_available) + self.min_interval;
    }
}

/// The default rate limiter: the SEC fair-access cap of 10 requests/second.
fn default_rate_limiter() -> RateLimiter {
    RateLimiter::new(DEFAULT_REQUESTS_PER_SECOND).expect("default rate")
}

/// Builder for [`ReferenceHTTPClient`].
pub struct ReferenceHTTPClientBuilder {
    user_agent: String,
    rate_limiter: Option<RateLimiter>,
    timeout_seconds: f64,
    max_attempts: u32,
    honor_retry_after: bool,
    backoff_seconds: f64,
    max_backoff_seconds: f64,
    transport: Option<Box<dyn ReferenceTransport>>,
    sleeper: Option<Box<dyn Sleeper>>,
    clock: Option<Box<dyn DatetimeClock>>,
}

impl ReferenceHTTPClientBuilder {
    /// Build with a user agent and the Python client defaults.
    pub fn new(user_agent: impl Into<String>) -> Self {
        Self {
            user_agent: user_agent.into(),
            rate_limiter: None,
            timeout_seconds: 30.0,
            max_attempts: 5,
            honor_retry_after: true,
            backoff_seconds: 0.5,
            max_backoff_seconds: 30.0,
            transport: None,
            sleeper: None,
            clock: None,
        }
    }

    /// Set the rate limiter (defaults to the SEC fair-access cap).
    pub fn rate_limiter(mut self, rate_limiter: RateLimiter) -> Self {
        self.rate_limiter = Some(rate_limiter);
        self
    }

    /// Set the whole-request timeout in seconds.
    pub fn timeout_seconds(mut self, timeout_seconds: f64) -> Self {
        self.timeout_seconds = timeout_seconds;
        self
    }

    /// Set the retry attempt budget.
    pub fn max_attempts(mut self, max_attempts: u32) -> Self {
        self.max_attempts = max_attempts;
        self
    }

    /// Set whether a 429 `Retry-After` header is honored.
    pub fn honor_retry_after(mut self, honor_retry_after: bool) -> Self {
        self.honor_retry_after = honor_retry_after;
        self
    }

    /// Set the exponential backoff base in seconds.
    pub fn backoff_seconds(mut self, backoff_seconds: f64) -> Self {
        self.backoff_seconds = backoff_seconds;
        self
    }

    /// Set the maximum backoff in seconds.
    pub fn max_backoff_seconds(mut self, max_backoff_seconds: f64) -> Self {
        self.max_backoff_seconds = max_backoff_seconds;
        self
    }

    /// Set an injectable transport (defaults to the reqwest transport).
    pub fn transport(mut self, transport: Box<dyn ReferenceTransport>) -> Self {
        self.transport = Some(transport);
        self
    }

    /// Set an injectable sleeper (defaults to tokio time).
    pub fn sleeper(mut self, sleeper: Box<dyn Sleeper>) -> Self {
        self.sleeper = Some(sleeper);
        self
    }

    /// Set an injectable wall clock (defaults to the system clock).
    pub fn clock(mut self, clock: Box<dyn DatetimeClock>) -> Self {
        self.clock = Some(clock);
        self
    }

    /// Validate and build the client.
    pub fn build(self) -> Result<ReferenceHTTPClient, ReferenceError> {
        let user_agent = self.user_agent.trim().to_string();
        if user_agent.is_empty() || !user_agent.contains('@') {
            return Err(ReferenceError::invalid(
                "user_agent must follow the SEC format \
                 'Sample Company Name AdminContact@domain.com'",
            ));
        }
        if self.timeout_seconds <= 0.0 {
            return Err(ReferenceError::invalid("timeout_seconds must be positive"));
        }
        if self.max_attempts < 1 {
            return Err(ReferenceError::invalid("max_attempts must be at least 1"));
        }
        if self.backoff_seconds < 0.0 {
            return Err(ReferenceError::invalid(
                "backoff_seconds cannot be negative",
            ));
        }
        if self.max_backoff_seconds < 0.0 {
            return Err(ReferenceError::invalid(
                "max_backoff_seconds cannot be negative",
            ));
        }
        let rate_limiter = self.rate_limiter.unwrap_or_else(default_rate_limiter);
        let transport = self
            .transport
            .unwrap_or_else(|| Box::new(ReqwestReferenceTransport::new(self.timeout_seconds)));
        let sleeper = self.sleeper.unwrap_or_else(|| Box::new(TokioSleeper));
        let clock = self.clock.unwrap_or_else(|| Box::new(SystemUtcClock));
        Ok(ReferenceHTTPClient {
            user_agent: user_agent.clone(),
            timeout_seconds: self.timeout_seconds,
            max_attempts: self.max_attempts,
            honor_retry_after: self.honor_retry_after,
            backoff_seconds: self.backoff_seconds,
            max_backoff_seconds: self.max_backoff_seconds,
            rate_limiter,
            transport,
            sleeper,
            clock,
            headers: vec![
                ("User-Agent".to_string(), user_agent),
                ("Accept-Encoding".to_string(), "gzip, deflate".to_string()),
            ],
        })
    }
}

/// Fetch reference payloads with fair-access pacing and bounded retries.
///
/// The SEC fair-access requirements are enforced by construction: every
/// request attempt consumes a [`RateLimiter`] slot (default 10 requests per
/// second, the cap the SEC policy sets) and carries the validated descriptive
/// `User-Agent` header.
pub struct ReferenceHTTPClient {
    user_agent: String,
    timeout_seconds: f64,
    max_attempts: u32,
    honor_retry_after: bool,
    backoff_seconds: f64,
    max_backoff_seconds: f64,
    rate_limiter: RateLimiter,
    transport: Box<dyn ReferenceTransport>,
    sleeper: Box<dyn Sleeper>,
    clock: Box<dyn DatetimeClock>,
    headers: Vec<(String, String)>,
}

impl core::fmt::Debug for ReferenceHTTPClient {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        formatter
            .debug_struct("ReferenceHTTPClient")
            .field("user_agent", &self.user_agent)
            .field("timeout_seconds", &self.timeout_seconds)
            .field("max_attempts", &self.max_attempts)
            .finish()
    }
}

impl ReferenceHTTPClient {
    /// Start a builder with the Python client defaults.
    pub fn builder(user_agent: impl Into<String>) -> ReferenceHTTPClientBuilder {
        ReferenceHTTPClientBuilder::new(user_agent)
    }

    /// Build a production client from validated reference settings.
    pub fn from_settings(
        user_agent: &str,
        settings: &crate::configuration::ReferenceHttpSettings,
    ) -> Self {
        Self::builder(user_agent)
            .timeout_seconds(f64::from(settings.timeout_seconds))
            .max_attempts(settings.retry.max_attempts)
            .honor_retry_after(settings.retry.honor_retry_after)
            .backoff_seconds(settings.retry.backoff_seconds)
            .max_backoff_seconds(settings.retry.max_backoff_seconds)
            .build()
            .expect("settings are validated")
    }

    /// The configured descriptive user agent.
    pub fn user_agent(&self) -> &str {
        &self.user_agent
    }

    /// The configured whole-request timeout in seconds.
    pub fn timeout_seconds(&self) -> f64 {
        self.timeout_seconds
    }

    /// The configured rate limiter rate in requests per second.
    pub fn requests_per_second(&self) -> f64 {
        self.rate_limiter.requests_per_second()
    }

    /// Fetch one payload with pacing, retries, and the User-Agent header.
    pub async fn get_bytes(&self, url: &str) -> Result<Vec<u8>, ReferenceError> {
        let response = self.request(url).await?;
        Ok(response.content)
    }

    /// Fetch and decode one JSON payload.
    pub async fn get_json(&self, url: &str) -> Result<Value, ReferenceError> {
        let response = self.request(url).await?;
        serde_json::from_slice(&response.content).map_err(|_| ReferenceError::Http {
            status: 200,
            url: url.to_string(),
        })
    }

    async fn request(&self, url: &str) -> Result<TransportResponse, ReferenceError> {
        for attempt in 1..=self.max_attempts {
            self.rate_limiter.acquire().await;
            match self
                .transport
                .request(url, &self.headers, self.timeout_seconds)
                .await
            {
                Err(failure) => {
                    if attempt == self.max_attempts {
                        return Err(ReferenceError::Transport(failure.message()));
                    }
                    self.sleep_by(self.backoff(attempt)).await;
                }
                Ok(response) => {
                    if (200..300).contains(&response.status) {
                        return Ok(response);
                    }
                    if !RETRYABLE_STATUS_CODES.contains(&response.status)
                        || attempt == self.max_attempts
                    {
                        return Err(ReferenceError::Http {
                            status: response.status,
                            url: url.to_string(),
                        });
                    }
                    let mut delay = self.backoff(attempt);
                    if response.status == 429 && self.honor_retry_after {
                        if let Some(retry_after) =
                            retry_after_seconds(&response.headers, self.clock.now())
                        {
                            delay = retry_after.min(self.max_backoff_seconds);
                        }
                    }
                    self.sleep_by(delay).await;
                }
            }
        }
        unreachable!("retry loop must return or raise")
    }

    async fn sleep_by(&self, seconds: f64) {
        self.sleeper.sleep(seconds).await;
    }

    fn backoff(&self, attempt: u32) -> f64 {
        (self.backoff_seconds * 2f64.powi(attempt as i32 - 1)).min(self.max_backoff_seconds)
    }
}

impl ReferenceFetchClient for ReferenceHTTPClient {
    fn get_bytes<'a>(
        &'a self,
        url: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ReferenceError>> + Send + 'a>> {
        Box::pin(self.get_bytes(url))
    }

    fn get_json<'a>(
        &'a self,
        url: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Value, ReferenceError>> + Send + 'a>> {
        Box::pin(self.get_json(url))
    }
}

/// Resolve a `Retry-After` header value into a non-negative delay in seconds.
///
/// Numeric seconds are taken directly; HTTP-date values are resolved against
/// `now`; unparsable values return `None` so the caller falls back to the
/// exponential backoff.
pub fn retry_after_seconds(headers: &[(String, String)], now: DateTime) -> Option<f64> {
    let value = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("retry-after"))
        .map(|(_, value)| value.trim().to_string())?;
    if let Ok(seconds) = value.parse::<f64>() {
        return if seconds.is_finite() {
            Some(seconds.max(0.0))
        } else {
            None
        };
    }
    let retry_at = parse_http_date(&value)?;
    let seconds = (retry_at.micros() - now.micros()) as f64 / 1_000_000.0;
    if !seconds.is_finite() {
        return None;
    }
    Some(seconds.max(0.0))
}

const MONTHS: [&str; 12] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
];

/// Parse an RFC 7231 HTTP-date (`Mon, 01 Jan 2024 00:00:10 GMT`, or the same
/// shape without the weekday name) into a UTC instant.
fn parse_http_date(value: &str) -> Option<DateTime> {
    let parts: Vec<&str> = value.split_whitespace().collect();
    let (day, month, year, time) = match parts.len() {
        6 => (parts[1], parts[2], parts[3], parts[4]),
        5 => (parts[0], parts[1], parts[2], parts[3]),
        _ => return None,
    };
    let day: u32 = day.trim_end_matches(',').parse().ok()?;
    let month = MONTHS
        .iter()
        .position(|name| name.eq_ignore_ascii_case(month.trim_end_matches(',')))
        .map(|index| index as u32 + 1)?;
    let year: i32 = year.trim_end_matches(',').parse().ok()?;
    let mut clock = time.split(':');
    let hour: i64 = clock.next()?.parse().ok()?;
    let minute: i64 = clock.next()?.parse().ok()?;
    let second: i64 = clock.next()?.parse().ok()?;
    let instant = DateTime::from_ymd_hms(
        year,
        month,
        day,
        u32::try_from(hour).ok()?,
        u32::try_from(minute).ok()?,
        u32::try_from(second).ok()?,
        0,
    )?;
    Some(instant)
}
