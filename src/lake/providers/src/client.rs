//! Paginated Alpaca REST client over an injectable transport.
//!
//! Ports `lake.providers.alpaca.client`: cursor following with repeated-token
//! detection, exponential backoff with 429 `Retry-After`, the inclusive end
//! bound, and first-failure cancellation across symbols. Beyond the Python
//! client it adds:
//!
//! * a process-wide [`RequestGovernor`] every attempt consumes (the hard
//!   requests-per-minute budget), and
//! * a per-key adaptive [`Lane`] that owns the in-flight bound: the fetch
//!   planner splits a window on demand into page-sized, exact, independent
//!   units so `bound` requests are in flight at all times, and the controller
//!   finds the highest sustainable rate the environment allows.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use futures_util::future::try_join_all;
use futures_util::stream::{self, FuturesUnordered, StreamExt, TryStreamExt};
use lake_contracts::{format_nanos_utc, parse_iso_nanos, DateTime};
use serde_json::Value;

use crate::config::ProviderConfig;
use crate::error::ProviderError;
use crate::governor::RequestGovernor;
use crate::lane::{lane_for, Lane};
use crate::parse::{self, DataType};
use crate::records::{
    NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedQuote, NormalizedTrade, Timestamped,
};
use crate::transport::{
    AlpacaTransport, BoxFuture, ReqwestTransport, TransportFailure, TransportResponse,
};

/// Alpaca's per-page ceiling.
const PAGE_SIZE: u32 = 10_000;
/// The only equity feed the lake requests.
const EQUITY_FEED: &str = "sip";
/// Statuses the retry policy treats as transient.
const RETRYABLE_STATUS_CODES: [u16; 7] = [408, 425, 429, 500, 502, 503, 504];
/// Initial fan-out granularity: one seed unit per this many nanoseconds.
const SEED_UNIT_NANOS: i64 = 60 * 1_000_000_000;
/// Upper bound on the seed fan-out per symbol window.
const MAX_SEEDS: usize = 64;

type Parser<T> = fn(&Value, &str) -> Result<T, ProviderError>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Market {
    Equity,
    Crypto,
}

/// Keep a row when it belongs to this slice: half-open except the final slice.
#[derive(Clone, Copy, Debug)]
struct SliceWindow {
    start: DateTime,
    end: DateTime,
    inclusive_end: bool,
}

impl SliceWindow {
    fn contains(self, timestamp: DateTime) -> bool {
        if timestamp < self.start || timestamp > self.end {
            return false;
        }
        timestamp < self.end || self.inclusive_end
    }
}

/// One fetch surface: market, data type, parser, and nanosecond bounds.
///
/// Bounds are carried at nanosecond precision because the split planner
/// advances a boundary by exactly one nanosecond past the last returned row;
/// microsecond rounding could drop same-instant ties.
struct FetchSpec<T> {
    market: Market,
    data_type: DataType,
    parser: Parser<T>,
    start: DateTime,
    end: DateTime,
    start_nanos: i64,
    end_nanos: i64,
    window: Option<SliceWindow>,
}

impl<T> FetchSpec<T> {
    fn new(
        market: Market,
        data_type: DataType,
        parser: Parser<T>,
        start: DateTime,
        end: DateTime,
        window: Option<SliceWindow>,
    ) -> Self {
        Self {
            market,
            data_type,
            parser,
            start,
            end,
            start_nanos: start.to_nanos_i64(),
            end_nanos: end.to_nanos_i64(),
            window,
        }
    }
}

/// The outcome of one fetch unit: its rows plus, when the page proved the
/// unit holds more than one page, the child units that cover the remainder.
struct UnitOutcome<T> {
    records: Vec<T>,
    children: Vec<(i64, i64)>,
}

/// One fetched page: its kept rows, the cursor, and the highest timestamp the
/// page proves fully fetched.
struct PageResult<T> {
    records: Vec<T>,
    next_page_token: Option<String>,
    safe_split_nanos: Option<i64>,
}

/// Paginated Alpaca client sharing one governor and one adaptive key lane.
pub struct AlpacaClient {
    config: ProviderConfig,
    transport: Arc<dyn AlpacaTransport>,
    governor: Arc<RequestGovernor>,
    lane: Arc<Lane>,
    headers: Vec<(String, String)>,
}

impl AlpacaClient {
    /// Build a client over an injected transport.
    ///
    /// Returns [`ProviderError::Configuration`] when `config.validate()` fails.
    pub fn new(
        config: ProviderConfig,
        transport: Arc<dyn AlpacaTransport>,
        governor: Arc<RequestGovernor>,
    ) -> Result<Self, ProviderError> {
        config.validate()?;
        let config = config.normalized();
        // One controller per API key: every client on the same key shares one
        // adaptive in-flight bound and learns one environment's ceiling.
        let lane = lane_for(
            config.api_key.as_deref(),
            config.requests_per_minute,
            config.max_concurrency,
        );
        let headers = provider_headers(&config);
        Ok(Self {
            config,
            transport,
            governor,
            lane,
            headers,
        })
    }

    /// Build a client over the production reqwest transport.
    pub fn with_reqwest(
        config: ProviderConfig,
        governor: Arc<RequestGovernor>,
    ) -> Result<Self, ProviderError> {
        config.validate()?;
        let transport = ReqwestTransport::new(config.timeout_seconds);
        Self::new(config, Arc::new(transport), governor)
    }

    /// The validated configuration this client uses.
    pub fn config(&self) -> &ProviderConfig {
        &self.config
    }

    /// The adaptive key lane this client fetches through.
    pub fn lane(&self) -> &Arc<Lane> {
        &self.lane
    }

    /// Fetch equity trades for the inclusive `[start, end]` window.
    pub async fn fetch_trades(
        &self,
        symbols: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<NormalizedTrade>, ProviderError> {
        let symbols = normalize_symbols(symbols)?;
        let spec = FetchSpec::new(
            Market::Equity,
            DataType::Trades,
            parse::parse_trade,
            start,
            end,
            None,
        );
        self.fetch_market(&symbols, &spec).await
    }

    /// Fetch equity quotes for the inclusive `[start, end]` window.
    pub async fn fetch_quotes(
        &self,
        symbols: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<NormalizedQuote>, ProviderError> {
        let symbols = normalize_symbols(symbols)?;
        let spec = FetchSpec::new(
            Market::Equity,
            DataType::Quotes,
            parse::parse_quote,
            start,
            end,
            None,
        );
        self.fetch_market(&symbols, &spec).await
    }

    /// Fetch crypto trades for the inclusive `[start, end]` window.
    pub async fn fetch_crypto_trades(
        &self,
        pairs: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<NormalizedCryptoTrade>, ProviderError> {
        let pairs = normalize_crypto_pairs(pairs)?;
        let spec = FetchSpec::new(
            Market::Crypto,
            DataType::Trades,
            parse::parse_crypto_trade,
            start,
            end,
            None,
        );
        self.fetch_market(&pairs, &spec).await
    }

    /// Fetch crypto quotes for the inclusive `[start, end]` window.
    pub async fn fetch_crypto_quotes(
        &self,
        pairs: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<NormalizedCryptoQuote>, ProviderError> {
        let pairs = normalize_crypto_pairs(pairs)?;
        let spec = FetchSpec::new(
            Market::Crypto,
            DataType::Quotes,
            parse::parse_crypto_quote,
            start,
            end,
            None,
        );
        self.fetch_market(&pairs, &spec).await
    }

    /// Fetch equity trades as `slices` concurrent sub-windows merged in
    /// timestamp order. `slices == 1` is plain sequential paging.
    pub async fn fetch_trades_sliced(
        &self,
        symbols: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
        slices: usize,
    ) -> Result<Vec<NormalizedTrade>, ProviderError> {
        let symbols = normalize_symbols(symbols)?;
        let spec = FetchSpec::new(
            Market::Equity,
            DataType::Trades,
            parse::parse_trade,
            start,
            end,
            None,
        );
        self.fetch_market_sliced(&symbols, &spec, slices).await
    }

    /// Fetch equity quotes as `slices` concurrent sub-windows merged in
    /// timestamp order. `slices == 1` is plain sequential paging.
    pub async fn fetch_quotes_sliced(
        &self,
        symbols: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
        slices: usize,
    ) -> Result<Vec<NormalizedQuote>, ProviderError> {
        let symbols = normalize_symbols(symbols)?;
        let spec = FetchSpec::new(
            Market::Equity,
            DataType::Quotes,
            parse::parse_quote,
            start,
            end,
            None,
        );
        self.fetch_market_sliced(&symbols, &spec, slices).await
    }

    async fn fetch_market<T: Timestamped + Send>(
        &self,
        symbols: &[String],
        spec: &FetchSpec<T>,
    ) -> Result<Vec<T>, ProviderError> {
        let tasks = symbols.iter().map(|symbol| self.fetch_symbol(symbol, spec));
        let pages = try_join_all(tasks).await?;
        Ok(pages.into_iter().flatten().collect())
    }

    async fn fetch_market_sliced<T: Timestamped + Send>(
        &self,
        symbols: &[String],
        spec: &FetchSpec<T>,
        slices: usize,
    ) -> Result<Vec<T>, ProviderError> {
        if slices == 0 {
            return Err(ProviderError::Configuration(
                "slices must be at least 1".to_string(),
            ));
        }
        if slices == 1 {
            // The explicit single-slice primitive is strictly sequential.
            let tasks = symbols
                .iter()
                .map(|symbol| self.fetch_symbol_sequential(symbol, spec));
            let pages = try_join_all(tasks).await?;
            return Ok(pages.into_iter().flatten().collect());
        }
        let windows = plan_time_slices(spec.start, spec.end, slices);
        let last = windows.len().saturating_sub(1);
        let fetches = windows
            .into_iter()
            .enumerate()
            .map(|(index, (start, end))| {
                let slice = FetchSpec::new(
                    spec.market,
                    spec.data_type,
                    spec.parser,
                    start,
                    end,
                    Some(SliceWindow {
                        start,
                        end,
                        inclusive_end: index == last,
                    }),
                );
                async move { self.fetch_market(symbols, &slice).await }
            });
        let pages: Vec<Vec<T>> = stream::iter(fetches)
            .buffer_unordered(self.config.max_concurrency)
            .try_collect()
            .await?;
        let mut records: Vec<T> = pages.into_iter().flatten().collect();
        records.sort_by_key(Timestamped::timestamp);
        Ok(records)
    }

    /// Fetch one symbol's window as independent, page-sized units.
    ///
    /// The window seeds into coarse units (one per minute, capped) for
    /// immediate fan-out; a unit whose page returns a cursor provably holds
    /// more than one page and splits its remainder at the last returned row
    /// plus one nanosecond. Adjacent units differ by exactly one nanosecond,
    /// so no timestamp falls between them and no row is fetched twice:
    /// requests equal pages exactly, with no probe and no density estimate.
    async fn fetch_symbol<T: Timestamped + Send>(
        &self,
        symbol: &str,
        spec: &FetchSpec<T>,
    ) -> Result<Vec<T>, ProviderError> {
        if spec.window.is_some() {
            return self.fetch_symbol_sequential(symbol, spec).await;
        }
        if spec.end_nanos <= spec.start_nanos {
            return self.fetch_symbol_sequential(symbol, spec).await;
        }
        let seed_count = usize::try_from((spec.end_nanos - spec.start_nanos) / SEED_UNIT_NANOS)
            .unwrap_or(usize::MAX)
            .clamp(1, MAX_SEEDS);
        let mut queue: FuturesUnordered<BoxFuture<'_, Result<UnitOutcome<T>, ProviderError>>> =
            FuturesUnordered::new();
        for (lower, upper) in partition_inclusive(spec.start_nanos, spec.end_nanos, seed_count) {
            queue.push(self.fetch_unit(symbol, spec, lower, upper));
        }
        let mut records: Vec<T> = Vec::new();
        while let Some(outcome) = queue.next().await {
            let outcome = outcome?;
            records.extend(outcome.records);
            for (lower, upper) in outcome.children {
                queue.push(self.fetch_unit(symbol, spec, lower, upper));
            }
        }
        records.sort_by_key(Timestamped::timestamp);
        Ok(records)
    }

    fn fetch_unit<'a, T: Timestamped + Send>(
        &'a self,
        symbol: &'a str,
        spec: &'a FetchSpec<T>,
        lower: i64,
        upper: i64,
    ) -> BoxFuture<'a, Result<UnitOutcome<T>, ProviderError>> {
        Box::pin(async move {
            let unit = FetchSpec {
                market: spec.market,
                data_type: spec.data_type,
                parser: spec.parser,
                start: DateTime::from_micros(lower.div_euclid(1_000)),
                end: DateTime::from_micros(upper.div_euclid(1_000)),
                start_nanos: lower,
                end_nanos: upper,
                window: None,
            };
            let page = self.fetch_page(symbol, &unit, None, true).await?;
            match page.next_page_token {
                None => Ok(UnitOutcome {
                    records: page.records,
                    children: Vec::new(),
                }),
                Some(token) if token.is_empty() => Ok(UnitOutcome {
                    records: page.records,
                    children: Vec::new(),
                }),
                Some(token) => {
                    // Split at the largest timestamp strictly below the page's
                    // final row: pages are contiguous and sorted, so every row
                    // at or below that timestamp is already fetched, while the
                    // trailing same-timestamp run may continue on the next page
                    // and is deliberately left to the children (which re-fetch
                    // it) instead of being orphaned by a one-nanosecond gap.
                    let remainder = page
                        .safe_split_nanos
                        .map(|safe| safe.saturating_add(1))
                        .filter(|start| *start <= upper);
                    let Some(remainder_start) = remainder else {
                        return self
                            .drain_cursor(symbol, &unit, page.records, token)
                            .await
                            .map(|all| UnitOutcome {
                                records: all,
                                children: Vec::new(),
                            });
                    };
                    let mid = remainder_start + (upper - remainder_start) / 2;
                    let mut children = vec![(remainder_start, mid)];
                    if mid < upper {
                        children.push((mid + 1, upper));
                    }
                    Ok(UnitOutcome {
                        records: page.records,
                        children,
                    })
                }
            }
        })
    }

    /// Follow a cursor to exhaustion when a unit cannot be split further.
    async fn drain_cursor<T: Timestamped>(
        &self,
        symbol: &str,
        spec: &FetchSpec<T>,
        mut records: Vec<T>,
        first_token: String,
    ) -> Result<Vec<T>, ProviderError> {
        let mut seen: HashSet<String> = HashSet::new();
        let mut token = Some(first_token);
        while let Some(current) = token {
            if !seen.insert(current.clone()) {
                return Err(ProviderError::Protocol(format!(
                    "repeated Alpaca {} page token for '{symbol}'",
                    spec.data_type.value()
                )));
            }
            let page = self.fetch_page(symbol, spec, Some(&current), false).await?;
            records.extend(page.records);
            token = page.next_page_token.filter(|value| !value.is_empty());
        }
        Ok(records)
    }

    async fn fetch_symbol_sequential<T: Timestamped>(
        &self,
        symbol: &str,
        spec: &FetchSpec<T>,
    ) -> Result<Vec<T>, ProviderError> {
        let mut page_token: Option<String> = None;
        let mut seen_tokens: HashSet<String> = HashSet::new();
        let mut records: Vec<T> = Vec::new();
        loop {
            let page = self
                .fetch_page(symbol, spec, page_token.as_deref(), false)
                .await?;
            records.extend(page.records);
            match page.next_page_token {
                None => return Ok(records),
                Some(token) if token.is_empty() => return Ok(records),
                Some(token) => {
                    if !seen_tokens.insert(token.clone()) {
                        return Err(ProviderError::Protocol(format!(
                            "repeated Alpaca {} page token for '{symbol}'",
                            spec.data_type.value()
                        )));
                    }
                    page_token = Some(token);
                }
            }
        }
    }

    async fn fetch_page<T: Timestamped>(
        &self,
        symbol: &str,
        spec: &FetchSpec<T>,
        page_token: Option<&str>,
        trim_trailing_ties: bool,
    ) -> Result<PageResult<T>, ProviderError> {
        let path = format!("{}/{}", market_path(spec.market), spec.data_type.value());
        let mut params = vec![
            ("start".to_string(), format_nanos_utc(spec.start_nanos)),
            ("end".to_string(), format_nanos_utc(spec.end_nanos)),
            ("limit".to_string(), PAGE_SIZE.to_string()),
            ("sort".to_string(), "asc".to_string()),
            ("symbols".to_string(), symbol.to_string()),
        ];
        if spec.market == Market::Equity {
            params.push(("feed".to_string(), EQUITY_FEED.to_string()));
        }
        if let Some(token) = page_token {
            params.push(("page_token".to_string(), token.to_string()));
        }
        let response = self.request(&path, &params).await?;
        let (rows, next_page_token) = parse::page_rows(&response.body, spec.data_type, symbol)?;
        // The raw timestamp order decides the split boundary: the largest
        // timestamp strictly below the page's final row is the last value for
        // which the page proves every existing row has been fetched.
        let mut last_nanos: Option<i64> = None;
        let mut safe_split_nanos: Option<i64> = None;
        for row in &rows {
            if let Some(nanos) = row
                .get("t")
                .and_then(Value::as_str)
                .and_then(parse_iso_nanos)
            {
                if last_nanos.is_some_and(|last| nanos != last) {
                    safe_split_nanos = last_nanos;
                }
                last_nanos = Some(nanos);
            }
        }
        let has_cursor = next_page_token
            .as_deref()
            .is_some_and(|token| !token.is_empty());
        let trim_below = if trim_trailing_ties && has_cursor {
            safe_split_nanos
        } else {
            None
        };
        let mut records = Vec::with_capacity(rows.len());
        for row in rows {
            if let Some(boundary) = trim_below {
                let nanos = row
                    .get("t")
                    .and_then(Value::as_str)
                    .and_then(parse_iso_nanos);
                if nanos.is_some_and(|nanos| nanos > boundary) {
                    continue;
                }
            }
            let record = (spec.parser)(row, symbol)?;
            let keep = match spec.window {
                Some(window) => window.contains(record.timestamp()),
                None => true,
            };
            if keep {
                records.push(record);
            }
        }
        Ok(PageResult {
            records,
            next_page_token,
            safe_split_nanos,
        })
    }

    async fn request(
        &self,
        path: &str,
        params: &[(String, String)],
    ) -> Result<TransportResponse, ProviderError> {
        let url = format!("{}{}", self.config.base_url, path);
        for attempt in 1..=self.config.max_attempts {
            self.governor.acquire().await;
            let permit = self.lane.acquire().await;
            let started = std::time::Instant::now();
            let outcome = self.transport.get(&url, params, &self.headers).await;
            let elapsed = started.elapsed();
            drop(permit);
            match outcome {
                Ok(response) => {
                    let status = response.status;
                    if (200..300).contains(&status) {
                        self.lane.complete(elapsed);
                        return Ok(response);
                    }
                    if !RETRYABLE_STATUS_CODES.contains(&status)
                        || attempt == self.config.max_attempts
                    {
                        return Err(ProviderError::Http { status, url });
                    }
                    tokio::time::sleep(self.retry_delay(&response, attempt)).await;
                }
                Err(TransportFailure::Transport(message)) => {
                    if attempt == self.config.max_attempts {
                        return Err(ProviderError::Transport(message));
                    }
                    tokio::time::sleep(self.backoff(attempt)).await;
                }
                Err(TransportFailure::Timeout(message)) => {
                    if attempt == self.config.max_attempts {
                        return Err(ProviderError::Timeout(message));
                    }
                    tokio::time::sleep(self.backoff(attempt)).await;
                }
                Err(TransportFailure::Protocol(message)) => {
                    return Err(ProviderError::Protocol(message));
                }
            }
        }
        unreachable!("the retry loop returns or raises on every path")
    }

    fn retry_delay(&self, response: &TransportResponse, attempt: u32) -> Duration {
        let mut delay = self.backoff_seconds(attempt);
        if response.status == 429 && self.config.honor_retry_after {
            if let Some(seconds) =
                retry_after_seconds(response.retry_after.as_deref(), now_utc_micros())
            {
                delay = seconds.min(self.config.max_backoff_seconds);
            }
        }
        Duration::from_secs_f64(delay.max(0.0))
    }

    fn backoff(&self, attempt: u32) -> Duration {
        Duration::from_secs_f64(self.backoff_seconds(attempt))
    }

    fn backoff_seconds(&self, attempt: u32) -> f64 {
        let exponent = i32::try_from(attempt.saturating_sub(1)).unwrap_or(i32::MAX);
        (self.config.backoff_seconds * 2f64.powi(exponent)).min(self.config.max_backoff_seconds)
    }
}

/// Partition `[start, end]` into `count` adjacent inclusive spans.
///
/// Unit `i` ends at `upper`; unit `i + 1` starts at `upper + 1`, so the union
/// covers every representable nanosecond exactly once and the child units of a
/// cursor split the same way.
fn partition_inclusive(start: i64, end: i64, count: usize) -> Vec<(i64, i64)> {
    if end <= start {
        return vec![(start, end)];
    }
    let span = end - start;
    // Units never shrink below one second: fanning a short window into
    // sub-second requests wastes budget without adding useful parallelism.
    let by_second = usize::try_from(span / 1_000_000_000)
        .unwrap_or(usize::MAX)
        .saturating_add(1);
    let possible = usize::try_from(span)
        .unwrap_or(usize::MAX)
        .saturating_add(1);
    let count = count.clamp(1, by_second.min(possible).max(1));
    if count == 1 {
        return vec![(start, end)];
    }
    let total = i64::try_from(count).unwrap_or(i64::MAX);
    let mut units = Vec::with_capacity(count);
    let mut lower = start;
    for index in 0..count {
        let upper = if index + 1 == count {
            end
        } else {
            (start + span / total * (i64::try_from(index).unwrap_or(0) + 1)).max(lower)
        };
        units.push((lower, upper));
        lower = upper.saturating_add(1);
    }
    if let Some(last) = units.last_mut() {
        last.1 = end;
    }
    units
}

/// Split `[start, end]` into `slices` contiguous sub-windows.
///
/// Every boundary except the final end is half-open for the consumer; the
/// final window includes `end`. Boundaries divide the microsecond span as
/// evenly as integer arithmetic allows, and slices share exact endpoints so
/// there is never a gap or overlap. Zero slices produce no windows; a
/// non-future span produces the single degenerate window `(start, end)`.
pub fn plan_time_slices(
    start: DateTime,
    end: DateTime,
    slices: usize,
) -> Vec<(DateTime, DateTime)> {
    if slices == 0 {
        return Vec::new();
    }
    if end.micros() <= start.micros() {
        return vec![(start, end)];
    }
    let start_micros = i128::from(start.micros());
    let end_micros = i128::from(end.micros());
    let span = end_micros - start_micros;
    let total = slices as i128;
    (0..slices)
        .map(|index| {
            let index = index as i128;
            let lower = start_micros + span * index / total;
            let upper = if index + 1 == total {
                end_micros
            } else {
                start_micros + span * (index + 1) / total
            };
            (
                DateTime::from_micros(lower as i64),
                DateTime::from_micros(upper as i64),
            )
        })
        .collect()
}

fn market_path(market: Market) -> &'static str {
    match market {
        Market::Equity => "/v2/stocks",
        Market::Crypto => "/v1beta3/crypto/us",
    }
}

fn provider_headers(config: &ProviderConfig) -> Vec<(String, String)> {
    let mut headers = vec![
        ("Accept".to_string(), "application/json".to_string()),
        // Compressed pages: the probe measured 10.5x fewer wire bytes for a
        // SPY quote page (1.16 MB -> 108 KB). reqwest's gzip/brotli features
        // decode transparently but only advertise when asked explicitly.
        ("Accept-Encoding".to_string(), "gzip, br".to_string()),
    ];
    if let Some(api_key) = &config.api_key {
        headers.push(("APCA-API-KEY-ID".to_string(), api_key.clone()));
    }
    if let Some(api_secret) = &config.api_secret {
        headers.push(("APCA-API-SECRET-KEY".to_string(), api_secret.clone()));
    }
    headers
}

fn normalize_symbols<S: AsRef<str>>(symbols: &[S]) -> Result<Vec<String>, ProviderError> {
    let mut result = Vec::with_capacity(symbols.len());
    let mut seen: HashSet<String> = HashSet::new();
    for value in symbols {
        let value = value.as_ref();
        let symbol = value.trim().to_uppercase();
        if symbol.is_empty() || symbol.chars().any(char::is_whitespace) {
            return Err(ProviderError::Configuration(format!(
                "invalid market symbol: '{value}'"
            )));
        }
        if seen.insert(symbol.clone()) {
            result.push(symbol);
        }
    }
    Ok(result)
}

fn normalize_crypto_pairs<S: AsRef<str>>(pairs: &[S]) -> Result<Vec<String>, ProviderError> {
    let mut result = Vec::with_capacity(pairs.len());
    let mut seen: HashSet<String> = HashSet::new();
    for value in pairs {
        let value = value.as_ref();
        let pair = value.trim().to_uppercase();
        if !is_crypto_pair(&pair) {
            return Err(ProviderError::Configuration(format!(
                "invalid crypto pair: '{value}'"
            )));
        }
        if seen.insert(pair.clone()) {
            result.push(pair);
        }
    }
    Ok(result)
}

fn is_crypto_pair(value: &str) -> bool {
    let Some((base, quote)) = value.split_once('/') else {
        return false;
    };
    let segment = |part: &str| {
        !part.is_empty()
            && part
                .bytes()
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
    };
    segment(base) && segment(quote)
}

/// Seconds until retry from a numeric or IMF-fixdate `Retry-After` header.
///
/// Returns `None` when the header is absent, is not finite, or cannot be
/// parsed; the caller then keeps the exponential backoff. Negative values
/// clamp to zero like the Python client.
fn retry_after_seconds(value: Option<&str>, now_micros: i64) -> Option<f64> {
    let value = value?.trim();
    if let Ok(seconds) = value.parse::<f64>() {
        return seconds.is_finite().then(|| seconds.max(0.0));
    }
    let retry_at = parse_http_date(value)?;
    let seconds = (retry_at.micros() - now_micros) as f64 / 1_000_000.0;
    Some(seconds.max(0.0))
}

fn now_utc_micros() -> i64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => i64::try_from(duration.as_micros()).unwrap_or(i64::MAX),
        Err(error) => -i64::try_from(error.duration().as_micros()).unwrap_or(i64::MAX),
    }
}

/// Parse the IMF-fixdate (and close RFC 2822) form used by `Retry-After`.
fn parse_http_date(value: &str) -> Option<DateTime> {
    let mut parts = value.split_whitespace();
    let mut first = parts.next()?;
    if first.ends_with(',') {
        first = parts.next()?;
    }
    let day: u32 = first.parse().ok()?;
    let month = month_number(parts.next()?)?;
    let year: i32 = parts.next()?.parse().ok()?;
    let mut clock = parts.next()?.split(':');
    let hour: u32 = clock.next()?.parse().ok()?;
    let minute: u32 = clock.next()?.parse().ok()?;
    let second: u32 = clock.next()?.parse().ok()?;
    if clock.next().is_some() {
        return None;
    }
    let offset_minutes = match parts.next() {
        None | Some("GMT") | Some("UTC") | Some("UT") => 0i64,
        Some(zone) => parse_numeric_offset(zone)?,
    };
    let date = DateTime::from_ymd_hms(year, month, day, hour, minute, second, 0)?;
    Some(DateTime::from_micros(
        date.micros() - offset_minutes * 60_000_000,
    ))
}

fn parse_numeric_offset(zone: &str) -> Option<i64> {
    let (sign, digits) = match zone.as_bytes().first() {
        Some(b'+') => (1i64, &zone[1..]),
        Some(b'-') => (-1i64, &zone[1..]),
        _ => return None,
    };
    if digits.len() != 4 || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let hours: i64 = digits[..2].parse().ok()?;
    let minutes: i64 = digits[2..].parse().ok()?;
    Some(sign * (hours * 60 + minutes))
}

fn month_number(name: &str) -> Option<u32> {
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    MONTHS
        .iter()
        .position(|month| month.eq_ignore_ascii_case(name))
        .map(|index| index as u32 + 1)
}

#[cfg(test)]
#[path = "../tests/unit/client.rs"]
mod tests;
