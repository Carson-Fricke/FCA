//! Alpaca endpoint, credential, retry, and budget settings.
//!
//! Mirrors the Python `AlpacaClient.__init__` keyword surface and the
//! `ProviderSettings` mapping, adding the process-wide `requests_per_minute`
//! budget the Rust fetch path drives the governor with.

use crate::error::ProviderError;

/// Validated Alpaca provider settings.
#[derive(Clone, Debug, PartialEq)]
pub struct ProviderConfig {
    /// Provider base URL, stored without a trailing slash.
    pub base_url: String,
    /// `APCA-API-KEY-ID` value; absent when unset.
    pub api_key: Option<String>,
    /// `APCA-API-SECRET-KEY` value; absent when unset.
    pub api_secret: Option<String>,
    /// Ceiling for the adaptive per-key in-flight bound. The lane controller
    /// ramps toward it while throughput improves and settles at the measured
    /// bandwidth-delay product; it is an upper bound, not a fixed setting.
    pub max_concurrency: usize,
    /// Whole-request timeout in seconds.
    pub timeout_seconds: f64,
    /// Attempts per request, including the first.
    pub max_attempts: u32,
    /// Whether a 429 `Retry-After` overrides the exponential backoff.
    pub honor_retry_after: bool,
    /// Base exponential backoff in seconds.
    pub backoff_seconds: f64,
    /// Backoff ceiling in seconds.
    pub max_backoff_seconds: f64,
    /// Process-wide request budget; `None` disables the governor.
    pub requests_per_minute: Option<u32>,
}

impl Default for ProviderConfig {
    fn default() -> Self {
        Self {
            base_url: "https://data.alpaca.markets".to_string(),
            api_key: None,
            api_secret: None,
            max_concurrency: 4,
            timeout_seconds: 30.0,
            max_attempts: 5,
            honor_retry_after: true,
            backoff_seconds: 0.25,
            max_backoff_seconds: 30.0,
            requests_per_minute: None,
        }
    }
}

impl ProviderConfig {
    /// Build a configuration with the provider defaults; the base URL is
    /// normalized without a trailing slash.
    pub fn new(
        base_url: impl Into<String>,
        api_key: Option<String>,
        api_secret: Option<String>,
    ) -> Self {
        Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            api_key,
            api_secret,
            ..Self::default()
        }
    }

    /// Validate every numeric bound, matching the Python constructor checks.
    pub fn validate(&self) -> Result<(), ProviderError> {
        if self.max_concurrency < 1 {
            return Err(configuration("max_concurrency must be at least 1"));
        }
        if !self.timeout_seconds.is_finite() || self.timeout_seconds <= 0.0 {
            return Err(configuration("timeout_seconds must be positive"));
        }
        if self.max_attempts < 1 {
            return Err(configuration("max_attempts must be at least 1"));
        }
        if !self.backoff_seconds.is_finite() || self.backoff_seconds < 0.0 {
            return Err(configuration("backoff_seconds cannot be negative"));
        }
        if !self.max_backoff_seconds.is_finite() || self.max_backoff_seconds < 0.0 {
            return Err(configuration("max_backoff_seconds cannot be negative"));
        }
        if self
            .requests_per_minute
            .is_some_and(|requests| !(1..=10_000).contains(&requests))
        {
            return Err(configuration(
                "requests_per_minute must be between 1 and 10000",
            ));
        }
        Ok(())
    }

    /// Normalize the base URL for request building.
    pub(crate) fn normalized(mut self) -> Self {
        self.base_url = self.base_url.trim_end_matches('/').to_string();
        self
    }
}

fn configuration(message: &str) -> ProviderError {
    ProviderError::Configuration(message.to_string())
}
