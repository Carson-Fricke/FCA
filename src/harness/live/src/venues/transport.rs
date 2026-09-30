//! Real async transport implementations for the live Alpaca broker boundary.
//!
//! The deterministic kernel never touches the network. Live I/O lives behind
//! the [`AsyncBrokerTransport`] and [`BrokerFrameStream`] traits: production
//! wraps `reqwest` (REST) and `tokio-tungstenite` (trade-update WebSocket),
//! while tests inject recording fakes so no network is ever required.
//!
//! [`AsyncBrokerTransport`]: crate::venues::alpaca::AsyncBrokerTransport

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use crate::error::{require_text, LiveError, LiveResult};
use crate::venues::alpaca::{AsyncBrokerTransport, TransportResponse};

/// Default Alpaca paper-trading REST base URL.
pub const ALPACA_PAPER_BASE_URL: &str = "https://paper-api.alpaca.markets";
/// Default Alpaca live-trading REST base URL.
pub const ALPACA_LIVE_BASE_URL: &str = "https://api.alpaca.markets";
/// Default Alpaca market-data streaming URL.
pub const ALPACA_STREAM_URL: &str = "wss://api.alpaca.markets/stream";

/// HTTP status codes the Alpaca REST clients retry after backoff.
pub const RETRYABLE_STATUS_CODES: [u16; 7] = [408, 425, 429, 500, 502, 503, 504];

/// A source of wall-clock time used by live venue adapters.
///
/// Kept intentionally narrow so tests can inject a deterministic clock and
/// production can use [`SystemClock`]; the synchronous kernel never sees it.
pub trait Clock: Send + Sync {
    /// Current wall-clock time as Unix milliseconds.
    fn now_millis(&self) -> i64;
}

/// Wall-clock [`Clock`] backed by `std::time::SystemTime`.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_millis(&self) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_millis() as i64)
            .unwrap_or_default()
    }
}

/// A boxed future returned by an injected sleep function.
pub type SleepFuture = Pin<Box<dyn Future<Output = ()> + Send + 'static>>;
/// An injected async sleep used by the retry loop.
pub type SleepFn = Arc<dyn Fn(f64) -> SleepFuture + Send + Sync>;

fn default_sleep() -> SleepFn {
    Arc::new(|seconds: f64| {
        let seconds = seconds.max(0.0);
        Box::pin(tokio::time::sleep(Duration::from_secs_f64(seconds)))
    })
}

/// Validated retry, timeout, and backoff policy for broker REST commands.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RetryConfig {
    /// Total attempts (at least 1) before a retryable failure is surfaced.
    pub max_attempts: usize,
    /// Base exponential backoff in seconds.
    pub backoff_seconds: f64,
    /// Upper bound applied to every backoff delay in seconds.
    pub max_backoff_seconds: f64,
    /// Whether a 429 `Retry-After` response header overrides the backoff.
    pub honor_retry_after: bool,
    /// Per-request timeout in seconds.
    pub timeout_seconds: f64,
}

impl RetryConfig {
    /// Construct a validated policy.
    pub fn new(
        max_attempts: usize,
        backoff_seconds: f64,
        max_backoff_seconds: f64,
        honor_retry_after: bool,
        timeout_seconds: f64,
    ) -> LiveResult<Self> {
        if max_attempts < 1 {
            return Err(LiveError::Venue("max_attempts must be at least 1".into()));
        }
        if !backoff_seconds.is_finite() || backoff_seconds < 0.0 {
            return Err(LiveError::Venue(
                "backoff_seconds must not be negative".into(),
            ));
        }
        if !max_backoff_seconds.is_finite() || max_backoff_seconds < 0.0 {
            return Err(LiveError::Venue(
                "max_backoff_seconds must not be negative".into(),
            ));
        }
        if !timeout_seconds.is_finite() || timeout_seconds <= 0.0 {
            return Err(LiveError::Venue("timeout_seconds must be positive".into()));
        }
        Ok(Self {
            max_attempts,
            backoff_seconds,
            max_backoff_seconds,
            honor_retry_after,
            timeout_seconds,
        })
    }

    /// Deterministic exponential backoff for the `attempt`-th (1-based) try.
    pub fn backoff(&self, attempt: usize) -> f64 {
        let exponent = attempt.saturating_sub(1) as i32;
        let delay = self.backoff_seconds * 2f64.powi(exponent);
        delay.min(self.max_backoff_seconds)
    }
}

impl Default for RetryConfig {
    fn default() -> Self {
        Self {
            max_attempts: 5,
            backoff_seconds: 0.25,
            max_backoff_seconds: 30.0,
            honor_retry_after: true,
            timeout_seconds: 30.0,
        }
    }
}

/// A transport wrapper that retries retryable HTTP and transport failures.
///
/// The inner transport performs exactly one request. This wrapper owns the
/// attempt, backoff, and `Retry-After` policy so every Alpaca adapter shares
/// identical live behavior; the sleep function is injectable for deterministic
/// tests.
pub struct RetryingBrokerTransport<T: AsyncBrokerTransport> {
    inner: T,
    config: RetryConfig,
    sleep: SleepFn,
}

impl<T: AsyncBrokerTransport> RetryingBrokerTransport<T> {
    /// Wrap `inner` with the supplied policy and the default tokio sleep.
    pub fn new(inner: T, config: RetryConfig) -> Self {
        Self {
            inner,
            config,
            sleep: default_sleep(),
        }
    }

    /// Wrap `inner` with an injected sleep function (used by tests).
    pub fn with_sleep(inner: T, config: RetryConfig, sleep: SleepFn) -> Self {
        Self {
            inner,
            config,
            sleep,
        }
    }

    /// The active retry policy.
    pub fn config(&self) -> RetryConfig {
        self.config
    }
}

impl<T: AsyncBrokerTransport> AsyncBrokerTransport for RetryingBrokerTransport<T> {
    async fn request(
        &self,
        method: &str,
        url: &str,
        body: Option<String>,
    ) -> LiveResult<TransportResponse> {
        for attempt in 1..=self.config.max_attempts {
            let last_attempt = attempt == self.config.max_attempts;
            match self.inner.request(method, url, body.clone()).await {
                Ok(response) if response.is_success() => return Ok(response),
                Ok(response) => {
                    if last_attempt || !RETRYABLE_STATUS_CODES.contains(&response.status) {
                        return Err(LiveError::Venue(format!(
                            "alpaca broker request failed with HTTP {}: {url}",
                            response.status
                        )));
                    }
                    let mut delay = self.config.backoff(attempt);
                    if response.status == 429 && self.config.honor_retry_after {
                        if let Some(retry_after) = response.retry_after_seconds() {
                            delay = retry_after.min(self.config.max_backoff_seconds);
                        }
                    }
                    (self.sleep)(delay).await;
                }
                Err(error) => {
                    if last_attempt {
                        return Err(error);
                    }
                    (self.sleep)(self.config.backoff(attempt)).await;
                }
            }
        }
        Err(LiveError::Venue(
            "alpaca retry loop exhausted without a response".into(),
        ))
    }
}

/// Real `reqwest` REST transport for Alpaca broker commands.
///
/// Owns one pooled HTTP client with a per-request timeout and the Alpaca API
/// key headers. It performs exactly one request per call; retry and backoff
/// live in [`RetryingBrokerTransport`] so they are testable without a network.
pub struct ReqwestBrokerTransport {
    client: reqwest::Client,
    headers: Vec<(String, String)>,
}

impl ReqwestBrokerTransport {
    /// Build a client for one Alpaca account.
    pub fn new(api_key: &str, api_secret: &str, timeout_seconds: f64) -> LiveResult<Self> {
        let api_key = require_text(api_key, "api_key")?;
        let api_secret = require_text(api_secret, "api_secret")?;
        if !timeout_seconds.is_finite() || timeout_seconds <= 0.0 {
            return Err(LiveError::Venue("timeout_seconds must be positive".into()));
        }
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs_f64(timeout_seconds))
            .build()
            .map_err(|error| {
                LiveError::Venue(format!("failed to build the Alpaca HTTP client: {error}"))
            })?;
        Ok(Self {
            client,
            headers: vec![
                ("APCA-API-KEY-ID".to_string(), api_key),
                ("APCA-API-SECRET-KEY".to_string(), api_secret),
                ("Accept".to_string(), "application/json".to_string()),
            ],
        })
    }
}

impl AsyncBrokerTransport for ReqwestBrokerTransport {
    async fn request(
        &self,
        method: &str,
        url: &str,
        body: Option<String>,
    ) -> LiveResult<TransportResponse> {
        let method = reqwest::Method::from_bytes(method.as_bytes()).map_err(|error| {
            LiveError::Venue(format!("invalid HTTP method {method:?}: {error}"))
        })?;
        let mut request = self.client.request(method, url);
        for (name, value) in &self.headers {
            request = request.header(name, value);
        }
        if let Some(body) = body {
            request = request
                .header("Content-Type", "application/json")
                .body(body);
        }
        let response = request.send().await.map_err(|error| {
            LiveError::Venue(format!("Alpaca transport request to {url} failed: {error}"))
        })?;
        let status = response.status().as_u16();
        let headers = response
            .headers()
            .iter()
            .map(|(name, value)| {
                (
                    name.as_str().to_string(),
                    value.to_str().unwrap_or_default().to_string(),
                )
            })
            .collect();
        let body = response.text().await.map_err(|error| {
            LiveError::Venue(format!("failed to read the Alpaca response body: {error}"))
        })?;
        Ok(TransportResponse {
            status,
            body,
            headers,
        })
    }
}

/// A decoded, ordered stream of broker text frames.
///
/// The inbox consumes decoded frames; this trait isolates the WebSocket
/// connection so normalization stays deterministic and network-free.
pub trait BrokerFrameStream {
    /// Return the next text frame, or `None` when the stream closes.
    fn next_text(&mut self) -> impl Future<Output = LiveResult<Option<String>>>;
}

/// A `tokio-tungstenite` WebSocket frame stream.
pub struct TungsteniteFrameStream {
    socket: tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
}

impl TungsteniteFrameStream {
    /// Open a WebSocket to `url`; TLS uses the bundled webpki roots.
    pub async fn connect(url: &str) -> LiveResult<Self> {
        let (socket, _response) = tokio_tungstenite::connect_async(url)
            .await
            .map_err(|error| {
                LiveError::Venue(format!(
                    "Alpaca WebSocket connection to {url} failed: {error}"
                ))
            })?;
        Ok(Self { socket })
    }
}

impl BrokerFrameStream for TungsteniteFrameStream {
    async fn next_text(&mut self) -> LiveResult<Option<String>> {
        use futures_util::StreamExt;

        use tokio_tungstenite::tungstenite::Message;
        loop {
            match self.socket.next().await {
                None => return Ok(None),
                Some(Err(error)) => {
                    return Err(LiveError::Venue(format!(
                        "Alpaca WebSocket stream error: {error}"
                    )))
                }
                Some(Ok(Message::Text(text))) => return Ok(Some(text.to_string())),
                Some(Ok(Message::Close(_))) => return Ok(None),
                Some(Ok(_)) => continue,
            }
        }
    }
}

/// A deterministic [`BrokerFrameStream`] over an in-memory frame script.
#[derive(Clone, Debug, Default)]
pub struct ScriptedFrameStream {
    frames: std::collections::VecDeque<String>,
    closed: bool,
}

impl ScriptedFrameStream {
    /// Construct a stream over an ordered frame script.
    pub fn new(frames: Vec<String>) -> Self {
        Self {
            frames: frames.into(),
            closed: false,
        }
    }
}

impl BrokerFrameStream for ScriptedFrameStream {
    async fn next_text(&mut self) -> LiveResult<Option<String>> {
        if self.closed {
            return Ok(None);
        }
        match self.frames.pop_front() {
            Some(frame) => Ok(Some(frame)),
            None => {
                self.closed = true;
                Ok(None)
            }
        }
    }
}
