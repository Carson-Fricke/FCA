//! Injectable HTTP transport and the production reqwest implementation.
//!
//! The trait uses a boxed future instead of trait `async fn` so that
//! `Arc<dyn AlpacaTransport>` stays object safe. Tests provide a fake
//! transport; production wraps a `reqwest::Client` carrying the configured
//! whole-request timeout.

use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use serde_json::Value;

/// A boxed, sendable future returned by [`AlpacaTransport::get`].
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// One provider response: status, `Retry-After`, and the decoded JSON body.
#[derive(Debug)]
pub struct TransportResponse {
    /// Response headroom retained through normalization; fake external transports leave this empty.
    pub allocation: Option<std::sync::Arc<lake_pipeline::Reservation>>,
    /// The HTTP status code.
    pub status: u16,
    /// The `Retry-After` header exactly as received.
    pub retry_after: Option<String>,
    /// The decoded JSON body.
    pub body: Value,
}

impl PartialEq for TransportResponse {
    fn eq(&self, other: &Self) -> bool {
        self.status == other.status
            && self.retry_after == other.retry_after
            && self.body == other.body
    }
}

/// A transport-level failure that never produced a usable response.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TransportFailure {
    /// The request could not be sent or completed.
    Transport(String),
    /// The request timed out.
    Timeout(String),
    /// The peer produced a malformed protocol response.
    Protocol(String),
}

/// Request surface implemented by the real client and by test fakes.
pub trait AlpacaTransport: Send + Sync {
    /// Send one GET request with query parameters and headers.
    fn get<'a>(
        &'a self,
        url: &'a str,
        params: &'a [(String, String)],
        headers: &'a [(String, String)],
        allocation: std::sync::Arc<lake_pipeline::Reservation>,
    ) -> BoxFuture<'a, Result<TransportResponse, TransportFailure>>;
}

/// Query encoding that matches Python's `quote` set: alphanumerics and
/// `-._~` stay verbatim, everything else is percent-encoded.
const QUERY_ENCODE_SET: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'.')
    .remove(b'_')
    .remove(b'~');

/// Production transport over a `reqwest::Client`.
pub struct ReqwestTransport {
    client: reqwest::Client,
}

impl ReqwestTransport {
    /// Build a transport with a whole-request timeout in seconds.
    ///
    /// Non-positive or non-finite timeouts fall back to the provider default
    /// of 30 seconds because `reqwest` rejects them outright.
    ///
    /// HTTP/1.1 only: HTTP/2 multiplexes every concurrent request through one
    /// TCP connection, and a few hundred parallel page requests then queue
    /// behind that connection's flow control (10-26s per 1 MB page measured).
    /// HTTP/1.1 gives each in-flight request its own pooled connection, so
    /// fetch bandwidth scales with the slice fan-out.
    pub fn new(timeout_seconds: f64) -> Self {
        let timeout = Duration::try_from_secs_f64(timeout_seconds)
            .unwrap_or_else(|_| Duration::from_secs(30));
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .http1_only()
            .pool_max_idle_per_host(1024)
            .build()
            .expect("reqwest client construction");
        Self { client }
    }
}

impl AlpacaTransport for ReqwestTransport {
    fn get<'a>(
        &'a self,
        url: &'a str,
        params: &'a [(String, String)],
        headers: &'a [(String, String)],
        allocation: std::sync::Arc<lake_pipeline::Reservation>,
    ) -> BoxFuture<'a, Result<TransportResponse, TransportFailure>> {
        Box::pin(async move {
            let mut request = self.client.get(encode_query(url, params));
            for (name, value) in headers {
                request = request.header(name.as_str(), value.as_str());
            }
            let pipeline = lake_pipeline::Pipeline::global();
            let mut network = pipeline.timer(lake_pipeline::Stage::Network);
            let mut response = match request.send().await {
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
            let retry_after = response
                .headers()
                .get(reqwest::header::RETRY_AFTER)
                .and_then(|value| value.to_str().ok())
                .map(str::to_string);
            let mut bytes = Vec::new();
            while let Some(chunk) = response
                .chunk()
                .await
                .map_err(|error| TransportFailure::Transport(error.to_string()))?
            {
                network.bytes(bytes.len().saturating_add(chunk.len()));
                pipeline
                    .check()
                    .map_err(|error| TransportFailure::Protocol(error.to_string()))?;
                if bytes.len().saturating_add(chunk.len()) > pipeline.limits.max_response_bytes {
                    return Err(TransportFailure::Protocol(
                        "provider response exceeds configured byte limit".into(),
                    ));
                }
                bytes.extend_from_slice(&chunk);
            }
            network.bytes(bytes.len());
            drop(network);
            let text = String::from_utf8(bytes)
                .map_err(|error| TransportFailure::Protocol(error.to_string()))?;
            // A full SIP page is CPU-heavy to decode. Keep JSON allocation and
            // parsing off the async workers needed by other in-flight requests.
            let cpu = pipeline
                .cpu()
                .await
                .map_err(|error| TransportFailure::Protocol(error.to_string()))?;
            let (body, allocation) = tokio::task::spawn_blocking(move || {
                let _cpu = cpu;
                let mut decode = pipeline.timer(lake_pipeline::Stage::Decode);
                decode.bytes(text.len());
                decode_body(&text, status).map(|body| (body, allocation))
            })
            .await
            .map_err(|error| {
                TransportFailure::Protocol(format!("response decode task failed: {error}"))
            })??;
            Ok(TransportResponse {
                allocation: Some(allocation),
                status,
                retry_after,
                body,
            })
        })
    }
}

/// Append the query parameters to `url` using the Python-compatible set.
pub(crate) fn encode_query(url: &str, params: &[(String, String)]) -> String {
    if params.is_empty() {
        return url.to_string();
    }
    let mut encoded = String::with_capacity(url.len() + params.len() * 24);
    encoded.push_str(url);
    encoded.push('?');
    for (index, (key, value)) in params.iter().enumerate() {
        if index > 0 {
            encoded.push('&');
        }
        encoded.push_str(&utf8_percent_encode(key, QUERY_ENCODE_SET).to_string());
        encoded.push('=');
        encoded.push_str(&utf8_percent_encode(value, QUERY_ENCODE_SET).to_string());
    }
    encoded
}

/// Decode a response body; successful responses must be JSON, error responses
/// are allowed to carry a non-JSON body because the status drives the policy.
fn decode_body(text: &str, status: u16) -> Result<Value, TransportFailure> {
    if text.is_empty() {
        return Ok(Value::Null);
    }
    match serde_json::from_str(text) {
        Ok(value) => Ok(value),
        Err(error) => {
            if (200..300).contains(&status) {
                Err(TransportFailure::Protocol(format!(
                    "Alpaca response body is not JSON: {error}"
                )))
            } else {
                Ok(Value::Null)
            }
        }
    }
}
