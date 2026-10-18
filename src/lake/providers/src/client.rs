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
use futures_util::stream::{self, FuturesUnordered, StreamExt};
use lake_catalog::ContractRecord;
use lake_contracts::{format_nanos_utc, AssetClass, BarInterval, DateTime};
use serde_json::Value;

use crate::config::ProviderConfig;
use crate::error::ProviderError;
use crate::governor::RequestGovernor;
use crate::lane::{lane_for, Lane};
use crate::parse::{self, DataType};
use crate::records::{
    NormalizedBar, NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedOptionQuote,
    NormalizedOptionTrade, NormalizedQuote, NormalizedTrade, RecordMemory, Timestamped,
};
use crate::transport::{
    AlpacaTransport, BoxFuture, ReqwestTransport, TransportFailure, TransportResponse,
};

/// Alpaca's per-page ceiling.
const PAGE_SIZE: u32 = 10_000;
/// Alpaca's per-page ceiling for option data.
const OPTION_PAGE_SIZE: u32 = 1_000;
/// Contracts bundled into one option request.
const OPTION_CONTRACTS_PER_REQUEST: usize = 100;
/// The only equity feed the lake requests.
const EQUITY_FEED: &str = "sip";
/// The only option feed the lake requests.
const OPTION_FEED: &str = "opra";
/// Statuses the retry policy treats as transient.
const RETRYABLE_STATUS_CODES: [u16; 7] = [408, 425, 429, 500, 502, 503, 504];
/// Initial fan-out granularity: one seed unit per this many nanoseconds.
const SEED_UNIT_NANOS: i64 = 60 * 1_000_000_000;
/// Upper bound on the seed fan-out per symbol window.
const MAX_SEEDS: usize = 64;

type Parser<T> = fn(&parse::RawRow<'_>, &str) -> Result<T, ProviderError>;

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
    fn copy(&self) -> Self {
        Self {
            market: self.market,
            data_type: self.data_type,
            parser: self.parser,
            start: self.start,
            end: self.end,
            start_nanos: self.start_nanos,
            end_nanos: self.end_nanos,
            window: self.window,
        }
    }
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

/// One fetched page: its kept rows, the cursor, and the highest timestamp the
/// page proves fully fetched.
struct PageResult<T> {
    allocation: std::sync::Arc<lake_pipeline::Reservation>,
    records: Vec<T>,
    next_page_token: Option<String>,
    safe_split_nanos: Option<i64>,
}

struct UnitPage<T> {
    page: PageResult<T>,
    unit: FetchSpec<T>,
    history: Option<HashSet<String>>,
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

    /// Acquire bounded provider pages; cancellation drops all pending requests.
    pub fn stream_trades<'a>(
        &'a self,
        symbols: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
    ) -> crate::RecordStream<'a, NormalizedTrade> {
        self.market_pages(
            normalize_symbols(symbols),
            FetchSpec::new(
                Market::Equity,
                DataType::Trades,
                parse::parse_trade,
                start,
                end,
                None,
            ),
        )
    }
    /// Acquire bounded provider pages; cancellation drops all pending requests.
    pub fn stream_quotes<'a>(
        &'a self,
        symbols: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
    ) -> crate::RecordStream<'a, NormalizedQuote> {
        self.market_pages(
            normalize_symbols(symbols),
            FetchSpec::new(
                Market::Equity,
                DataType::Quotes,
                parse::parse_quote,
                start,
                end,
                None,
            ),
        )
    }
    /// Acquire bounded provider pages; cancellation drops all pending requests.
    pub fn stream_crypto_trades<'a>(
        &'a self,
        symbols: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
    ) -> crate::RecordStream<'a, NormalizedCryptoTrade> {
        self.market_pages(
            normalize_crypto_pairs(symbols),
            FetchSpec::new(
                Market::Crypto,
                DataType::Trades,
                parse::parse_crypto_trade,
                start,
                end,
                None,
            ),
        )
    }
    /// Acquire bounded provider pages; cancellation drops all pending requests.
    pub fn stream_crypto_quotes<'a>(
        &'a self,
        symbols: &[impl AsRef<str>],
        start: DateTime,
        end: DateTime,
    ) -> crate::RecordStream<'a, NormalizedCryptoQuote> {
        self.market_pages(
            normalize_crypto_pairs(symbols),
            FetchSpec::new(
                Market::Crypto,
                DataType::Quotes,
                parse::parse_crypto_quote,
                start,
                end,
                None,
            ),
        )
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

    /// Fetch one symbol's session-aligned OHLCV bars for the inclusive
    /// `[start, end]` window.
    ///
    /// Equities use `/v2/stocks/{symbol}/bars` with the SIP feed and raw
    /// adjustment; crypto uses `/v1beta3/crypto/us/bars` (crypto bars are
    /// inherently unadjusted). Paging follows the provider's next page token
    /// through the same governor, key lane, and retry path as every other
    /// fetch; records come back in timestamp order. The interval selects the
    /// provider timeframe (`1Min` / `1Day`).
    pub async fn fetch_bars(
        &self,
        asset_class: AssetClass,
        symbol: &str,
        interval: BarInterval,
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<NormalizedBar>, ProviderError> {
        let symbol = match asset_class {
            AssetClass::Equity | AssetClass::Option => {
                normalize_symbols(std::slice::from_ref(&symbol))?
                    .into_iter()
                    .next()
                    .expect("one normalized symbol")
            }
            AssetClass::Crypto => normalize_crypto_pairs(std::slice::from_ref(&symbol))?
                .into_iter()
                .next()
                .expect("one normalized pair"),
            other => {
                return Err(ProviderError::Configuration(format!(
                    "bars are unsupported for asset class {}",
                    other.value()
                )))
            }
        };
        let path = match asset_class {
            AssetClass::Equity => format!("/v2/stocks/{symbol}/bars"),
            AssetClass::Option => "/v1beta1/options/bars".to_string(),
            _ => "/v1beta3/crypto/us/bars".to_string(),
        };
        let base_params = match asset_class {
            AssetClass::Equity => vec![
                ("start".to_string(), format_nanos_utc(start.to_nanos_i64())),
                ("end".to_string(), format_nanos_utc(end.to_nanos_i64())),
                ("limit".to_string(), PAGE_SIZE.to_string()),
                ("sort".to_string(), "asc".to_string()),
                (
                    "timeframe".to_string(),
                    timeframe_value(interval).to_string(),
                ),
                ("adjustment".to_string(), "raw".to_string()),
                ("feed".to_string(), EQUITY_FEED.to_string()),
            ],
            _ => vec![
                ("symbols".to_string(), symbol.clone()),
                ("start".to_string(), format_nanos_utc(start.to_nanos_i64())),
                ("end".to_string(), format_nanos_utc(end.to_nanos_i64())),
                ("limit".to_string(), PAGE_SIZE.to_string()),
                ("sort".to_string(), "asc".to_string()),
                (
                    "timeframe".to_string(),
                    timeframe_value(interval).to_string(),
                ),
            ],
        };
        let mut page_token: Option<String> = None;
        let mut seen_tokens: HashSet<String> = HashSet::new();
        let mut records: Vec<NormalizedBar> = Vec::new();
        let mut retained_bytes = 0;
        loop {
            let mut params = base_params.clone();
            if let Some(token) = &page_token {
                params.push(("page_token".to_string(), token.clone()));
            }
            let response = self.request(&path, &params).await?;
            let requested_symbol = symbol.clone();
            let pipeline = lake_pipeline::Pipeline::global();
            let cpu = pipeline.cpu().await.map_err(resource_error)?;
            let (page, next_page_token) = tokio::task::spawn_blocking(move || {
                let _cpu = cpu;
                let mut timer = pipeline.timer(lake_pipeline::Stage::Normalize);
                let (rows, next) =
                    parse::page_rows(&response.body, DataType::Bars, &requested_symbol)?;
                let page = rows
                    .into_iter()
                    .map(|row| parse::parse_bar(&row, &requested_symbol))
                    .collect::<Result<Vec<_>, _>>()?;
                timer.rows(page.len());
                Ok::<_, ProviderError>((page, next))
            })
            .await
            .map_err(|e| ProviderError::Protocol(e.to_string()))??;
            append_records(&mut records, page, &mut retained_bytes)?;
            match next_page_token {
                None => break,
                Some(token) if token.is_empty() => break,
                Some(token) => {
                    if !seen_tokens.insert(token.clone()) {
                        return Err(ProviderError::Protocol(format!(
                            "repeated Alpaca bars page token for '{symbol}'"
                        )));
                    }
                    page_token = Some(token);
                }
            }
        }
        sort_records(records).await
    }

    /// Fetch the complete contract master in an explicit expiry window.
    /// Both active and inactive contracts are included; historical quote
    /// evidence, rather than today's active status, establishes eligibility.
    pub async fn fetch_option_contracts(
        &self,
        trading_base_url: &str,
        underlying: &str,
        start: lake_contracts::Date,
        end: lake_contracts::Date,
    ) -> Result<Vec<ContractRecord>, ProviderError> {
        let mut contracts = std::collections::BTreeMap::new();
        let url = format!(
            "{}/v2/options/contracts",
            trading_base_url.trim_end_matches('/')
        );
        for status in ["active", "inactive"] {
            let mut token: Option<String> = None;
            let mut seen = HashSet::new();
            loop {
                let mut params = vec![
                    ("underlying_symbols".into(), underlying.to_string()),
                    ("expiration_date_gte".into(), start.isoformat()),
                    ("expiration_date_lte".into(), end.isoformat()),
                    ("status".into(), status.into()),
                    ("limit".into(), "1000".into()),
                ];
                if let Some(token) = &token {
                    params.push(("page_token".into(), token.clone()));
                }
                let response = self.request_url(&url, &params).await?;
                let body: Value = serde_json::from_slice(&response.body)
                    .map_err(|e| ProviderError::Protocol(e.to_string()))?;
                let rows = body
                    .get("option_contracts")
                    .and_then(Value::as_array)
                    .ok_or_else(|| {
                        ProviderError::Protocol("missing option_contracts array".into())
                    })?;
                for row in rows {
                    let text = |key: &str| {
                        row.get(key).and_then(Value::as_str).ok_or_else(|| {
                            ProviderError::Protocol(format!("missing contract {key}"))
                        })
                    };
                    let expiration = lake_contracts::Date::parse(text("expiration_date")?)
                        .ok_or_else(|| {
                            ProviderError::Protocol("invalid contract expiration".into())
                        })?;
                    let right = match text("type")? {
                        "call" => lake_catalog::OptionType::Call,
                        "put" => lake_catalog::OptionType::Put,
                        _ => return Err(ProviderError::Protocol("invalid contract right".into())),
                    };
                    let multiplier = text("size")?.parse::<u32>().map_err(|_| {
                        ProviderError::Protocol("invalid contract multiplier".into())
                    })?;
                    let record = ContractRecord::new(
                        text("symbol")?,
                        text("underlying_symbol")?,
                        expiration,
                        text("strike_price")?
                            .parse()
                            .map_err(|_| ProviderError::Protocol("invalid strike".into()))?,
                        right,
                        multiplier,
                        None,
                        None,
                    )
                    .map_err(|e| ProviderError::Protocol(e.to_string()))?;
                    if record.underlying != underlying || expiration < start || expiration > end {
                        return Err(ProviderError::Protocol(
                            "contract outside requested universe".into(),
                        ));
                    }
                    contracts.insert(record.symbol.clone(), record);
                }
                token = match body.get("next_page_token") {
                    None | Some(Value::Null) => None,
                    Some(Value::String(s)) if s.is_empty() => None,
                    Some(Value::String(s)) => Some(s.clone()),
                    _ => return Err(ProviderError::Protocol("invalid contract cursor".into())),
                };
                let Some(next) = &token else {
                    break;
                };
                if !seen.insert(next.clone()) {
                    return Err(ProviderError::Protocol("repeated contract cursor".into()));
                }
            }
        }
        Ok(contracts.into_values().collect())
    }

    /// Fetch historical OPRA trades for selected option contracts.
    ///
    /// Contracts are deduplicated by OSI symbol, batched 100 per request, and
    /// each batch follows its cursor sequentially: the option endpoints do not
    /// participate in the equity split planner. Every attempt consumes the
    /// governor and the shared per-key lane through the same request path as
    /// the equity and crypto fetches.
    pub async fn fetch_option_trades(
        &self,
        contracts: &[ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<NormalizedOptionTrade>, ProviderError> {
        self.fetch_option_data(
            contracts,
            DataType::Trades,
            start,
            end,
            parse::parse_option_trade,
        )
        .await
    }

    /// Fetch historical OPRA quotes for selected option contracts.
    pub async fn fetch_option_quotes(
        &self,
        contracts: &[ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> Result<Vec<NormalizedOptionQuote>, ProviderError> {
        self.fetch_option_data(
            contracts,
            DataType::Quotes,
            start,
            end,
            parse::parse_option_quote,
        )
        .await
    }

    pub fn stream_option_trades<'a>(
        &'a self,
        contracts: &[ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> crate::RecordStream<'a, NormalizedOptionTrade> {
        self.option_pages(
            contracts,
            DataType::Trades,
            start,
            end,
            parse::parse_option_trade,
        )
    }
    pub fn stream_option_quotes<'a>(
        &'a self,
        contracts: &[ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> crate::RecordStream<'a, NormalizedOptionQuote> {
        self.option_pages(
            contracts,
            DataType::Quotes,
            start,
            end,
            parse::parse_option_quote,
        )
    }
    fn option_pages<'a, T: RecordMemory + Send + 'static>(
        &'a self,
        contracts: &[ContractRecord],
        data_type: DataType,
        start: DateTime,
        end: DateTime,
        parser: fn(&parse::RawRow<'_>, &ContractRecord) -> Result<T, ProviderError>,
    ) -> crate::RecordStream<'a, T> {
        let contracts = normalize_contracts(contracts);
        Box::pin(async_stream::try_stream! {
            for batch in contracts.chunks(OPTION_CONTRACTS_PER_REQUEST){
                let mut pages=self.fetch_option_batch(batch.to_vec(),data_type,start,end,parser);
                while let Some(page)=pages.next().await {yield page?;}
            }
        })
    }
    async fn fetch_option_data<T: RecordMemory + Send + 'static>(
        &self,
        contracts: &[ContractRecord],
        data_type: DataType,
        start: DateTime,
        end: DateTime,
        parser: fn(&parse::RawRow<'_>, &ContractRecord) -> Result<T, ProviderError>,
    ) -> Result<Vec<T>, ProviderError> {
        let mut pages = self.option_pages(contracts, data_type, start, end, parser);
        let mut records = Vec::new();
        let mut bytes = 0;
        while let Some(page) = pages.next().await {
            append_records(&mut records, page?.records, &mut bytes)?;
        }
        Ok(records)
    }

    /// Fetch one option batch, paging its cursor to exhaustion.
    fn fetch_option_batch<'a, T: RecordMemory + Send + 'static>(
        &'a self,
        batch: Vec<(String, ContractRecord)>,
        data_type: DataType,
        start: DateTime,
        end: DateTime,
        parser: fn(&parse::RawRow<'_>, &ContractRecord) -> Result<T, ProviderError>,
    ) -> crate::RecordStream<'a, T> {
        Box::pin(async_stream::try_stream! {
        let symbols = batch
            .iter()
            .map(|(symbol, _)| symbol.as_str())
            .collect::<Vec<_>>()
            .join(",");
        let base_params = vec![
            ("symbols".to_string(), symbols),
            ("start".to_string(), format_nanos_utc(start.to_nanos_i64())),
            ("end".to_string(), format_nanos_utc(end.to_nanos_i64())),
            ("limit".to_string(), OPTION_PAGE_SIZE.to_string()),
            ("sort".to_string(), "asc".to_string()),
        ];
        let path = format!("/v1beta1/options/{}", data_type.value());
        let mut page_token: Option<String> = None;
        let mut seen_tokens: HashSet<String> = HashSet::new();
        loop {
            let mut params = base_params.clone();
            // The provider's option trades endpoint rejects a feed parameter
            // outright ("unexpected query parameter(s): feed"); only quotes
            // accept one. The feed stays pinned to "opra" by configuration.
            if data_type == DataType::Quotes {
                params.push(("feed".to_string(), OPTION_FEED.to_string()));
            }
            if let Some(token) = &page_token {
                params.push(("page_token".to_string(), token.clone()));
            }
            let response = self.request(&path, &params).await?;
            let batch = batch.to_vec();
            let decoded=std::sync::Arc::new(lake_pipeline::Pipeline::global().decoded_response(response.body.len()).await.map_err(resource_error)?);
            let pipeline = lake_pipeline::Pipeline::global();
            let cpu = pipeline.cpu().await.map_err(resource_error)?;
            let (page, next_page_token) = tokio::task::spawn_blocking(move || {
                let _cpu = cpu;
                let decode=pipeline.timer(lake_pipeline::Stage::Decode);
                let (rows, next) = parse::option_page_rows(&response.body, data_type, &batch)?;drop(decode);
                let mut timer = pipeline.timer(lake_pipeline::Stage::Normalize);
                let page = rows
                    .into_iter()
                    .map(|(contract, row)| parser(&row, contract))
                    .collect::<Result<Vec<_>, _>>()?;
                timer.rows(page.len());
                Ok::<_, ProviderError>((page, next))
            })
            .await
            .map_err(|e| ProviderError::Protocol(e.to_string()))??;
            yield crate::RecordPage{records:page,allocation:Some(decoded)};
            match next_page_token {
                None => break,
                Some(token) if token.is_empty() => break,
                Some(token) => {
                    if !seen_tokens.insert(token.clone()) {
                        Err(ProviderError::Protocol(format!(
                            "repeated Alpaca option {} page token",
                            data_type.value()
                        )))?;
                    }
                    page_token = Some(token);
                }
            }
        }
            })
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

    async fn fetch_market<T: Timestamped + Send + 'static>(
        &self,
        symbols: &[String],
        spec: &FetchSpec<T>,
    ) -> Result<Vec<T>, ProviderError> {
        let mut pending = self.market_pages(Ok(symbols.to_vec()), spec.copy());
        let mut records = Vec::new();
        let mut bytes = 0;
        while let Some(page) = pending.next().await {
            append_records(&mut records, page?.records, &mut bytes)?;
        }
        sort_records(records).await
    }

    async fn fetch_market_sliced<T: Timestamped + Send + 'static>(
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
        let mut pending = stream::iter(fetches).buffer_unordered(self.config.max_concurrency);
        let mut records = Vec::new();
        let mut retained_bytes = 0;
        while let Some(page) = pending.next().await {
            append_records(&mut records, page?, &mut retained_bytes)?;
        }
        sort_records(records).await
    }

    fn market_pages<'a, T: Timestamped + Send + 'static>(
        &'a self,
        symbols: Result<Vec<String>, ProviderError>,
        spec: FetchSpec<T>,
    ) -> crate::RecordStream<'a, T> {
        Box::pin(async_stream::try_stream! {
            let streams=symbols?.into_iter()
                .map(|symbol| self.symbol_pages(symbol,spec.copy())).collect::<Vec<_>>();
            let mut pages=stream::select_all(streams);
            while let Some(page)=pages.next().await {
                yield page?;
            }
        })
    }

    /// Fetch one symbol's window as independent, page-sized units.
    ///
    /// The window seeds into coarse units (one per minute, capped) for
    /// immediate fan-out; a unit whose page returns a cursor provably holds
    /// more than one page and splits its remainder at the last returned row
    /// plus one nanosecond. Adjacent units differ by exactly one nanosecond,
    /// so no timestamp falls between them and no row is fetched twice:
    /// requests equal pages exactly, with no probe and no density estimate.
    fn symbol_pages<'a, T: Timestamped + Send + 'static>(
        &'a self,
        symbol: String,
        spec: FetchSpec<T>,
    ) -> crate::RecordStream<'a, T> {
        Box::pin(async_stream::try_stream! {
            let seeds=if spec.window.is_some() {1} else {
                usize::try_from((spec.end_nanos-spec.start_nanos).max(0)/SEED_UNIT_NANOS)
                    .unwrap_or(MAX_SEEDS).clamp(1,MAX_SEEDS)
            };
            let mut pending:FuturesUnordered<BoxFuture<'_,Result<UnitPage<T>,ProviderError>>>=FuturesUnordered::new();
            for (lower,upper) in partition_inclusive(spec.start_nanos,spec.end_nanos,seeds) {
                let unit=FetchSpec {start_nanos:lower,end_nanos:upper,..spec.copy()};
                pending.push(Box::pin(self.fetch_unit(&symbol,unit,None,None,true)));
            }
            while let Some(result)=pending.next().await {
                let UnitPage {mut page,unit,history}=result?;
                let cursor=page.next_page_token.take().filter(|token| !token.is_empty());
                let remainder=if history.is_none() && spec.window.is_none() {
                    page.safe_split_nanos.map(|value| value.saturating_add(1))
                        .filter(|value| *value<=unit.end_nanos)
                } else {None};
                if !page.records.is_empty() {
                    yield crate::RecordPage {records:page.records,allocation:Some(page.allocation)};
                }
                if let Some(token)=cursor {
                    if let Some(lower)=remainder {
                        let mid=lower+(unit.end_nanos-lower)/2;
                        for (start,end) in [(lower,mid),(mid.saturating_add(1),unit.end_nanos)] {
                            if start>end {continue;}
                            let child=FetchSpec {start_nanos:start,end_nanos:end,..unit.copy()};
                            pending.push(Box::pin(self.fetch_unit(&symbol,child,None,None,true)));
                        }
                    } else {
                        let mut seen=history.unwrap_or_default();
                        if !seen.insert(token.clone()) {
                            Err(ProviderError::Protocol("repeated Alpaca page token".into()))?;
                        }
                        pending.push(Box::pin(self.fetch_unit(&symbol,unit,Some(token),Some(seen),false)));
                    }
                }
            }
        })
    }
    async fn fetch_unit<T: Timestamped + Send + 'static>(
        &self,
        symbol: &str,
        unit: FetchSpec<T>,
        cursor: Option<String>,
        history: Option<HashSet<String>>,
        trim: bool,
    ) -> Result<UnitPage<T>, ProviderError> {
        let page = self
            .fetch_page(symbol, &unit, cursor.as_deref(), trim)
            .await?;
        Ok(UnitPage {
            page,
            unit,
            history,
        })
    }

    async fn fetch_symbol_sequential<T: Timestamped + Send + 'static>(
        &self,
        symbol: &str,
        spec: &FetchSpec<T>,
    ) -> Result<Vec<T>, ProviderError> {
        let mut spec = spec.copy();
        spec.window = Some(SliceWindow {
            start: spec.start,
            end: spec.end,
            inclusive_end: true,
        });
        let mut pages = self.symbol_pages(symbol.into(), spec);
        let mut records = Vec::new();
        let mut bytes = 0;
        while let Some(page) = pages.next().await {
            append_records(&mut records, page?.records, &mut bytes)?;
        }
        Ok(records)
    }

    async fn fetch_page<T: Timestamped + Send + 'static>(
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
        // The row scan, boundary trim, and per-row conversion are the CPU hot
        // path (millions of JSON rows per session). Move the body and run the
        // parse on the blocking pool so page parses from concurrent units
        // execute in parallel on the runtime's workers instead of serializing
        // inside one fetch task.
        let body = response.body;
        let decoded = std::sync::Arc::new(
            lake_pipeline::Pipeline::global()
                .decoded_response(body.len())
                .await
                .map_err(resource_error)?,
        );
        let allocation = response.allocation;
        let parser = spec.parser;
        let window = spec.window;
        let data_type = spec.data_type;
        let symbol = symbol.to_string();
        let parse_started = std::time::Instant::now();
        let pipeline = lake_pipeline::Pipeline::global();
        let cpu = pipeline
            .cpu()
            .await
            .map_err(|error| ProviderError::Configuration(error.to_string()))?;
        let (parsed, next_page_token) = tokio::task::spawn_blocking(
            move || -> Result<(ParsedPage<T>, Option<String>), ProviderError> {
                let _cpu = cpu;
                let _allocation = allocation;
                let decode = pipeline.timer(lake_pipeline::Stage::Decode);
                let (rows, next_page_token) = parse::page_rows(&body, data_type, &symbol)?;
                drop(decode);
                let mut normalize = pipeline.timer(lake_pipeline::Stage::Normalize);
                let has_cursor = next_page_token
                    .as_deref()
                    .is_some_and(|token| !token.is_empty());
                let page = parse_page_rows(
                    &rows,
                    parser,
                    window,
                    trim_trailing_ties && has_cursor,
                    &symbol,
                )?;
                normalize.rows(page.records.len());
                Ok((page, next_page_token))
            },
        )
        .await
        .map_err(|error| ProviderError::Transport(format!("page parse task failed: {error}")))??;
        // Parsing is service time on this page's critical path. Feed it back to
        // the lane so the in-flight bound reflects the full page cost instead
        // of transport latency alone (which under-provisions concurrency and
        // collapses throughput to the controller floor).
        self.lane.observe_service(parse_started.elapsed());
        Ok(PageResult {
            allocation: decoded,
            records: parsed.records,
            next_page_token,
            safe_split_nanos: parsed.safe_split_nanos,
        })
    }

    async fn request(
        &self,
        path: &str,
        params: &[(String, String)],
    ) -> Result<TransportResponse, ProviderError> {
        let url = format!("{}{}", self.config.base_url, path);
        self.request_url(&url, params).await
    }

    async fn request_url(
        &self,
        url: &str,
        params: &[(String, String)],
    ) -> Result<TransportResponse, ProviderError> {
        for attempt in 1..=self.config.max_attempts {
            let pipeline = lake_pipeline::Pipeline::global();
            let allocation = std::sync::Arc::new(
                pipeline
                    .response()
                    .await
                    .map_err(|error| ProviderError::Configuration(error.to_string()))?,
            );
            let permit = self.lane.acquire().await;
            let rate_wait = pipeline.timer(lake_pipeline::Stage::RateWait);
            self.governor.before_request().await?;
            drop(rate_wait);
            let started = std::time::Instant::now();
            let outcome = self
                .transport
                .get(url, params, &self.headers, allocation.clone())
                .await
                .map(|mut response| {
                    response.allocation = Some(allocation);
                    response
                });
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
                        return Err(ProviderError::Http {
                            status,
                            url: url.to_string(),
                        });
                    }
                    let delay = self.retry_delay(&response, attempt);
                    drop(response);
                    tokio::time::sleep(delay).await;
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

/// The CPU-only half of one fetched page: the converted records and the
/// boundary a cursor-bearing page may split at.
struct ParsedPage<T> {
    records: Vec<T>,
    safe_split_nanos: Option<i64>,
}

/// Scan, trim, and convert one page's raw rows.
///
/// This is the parsing hot path; callers run it on the blocking pool so
/// concurrent page fetches parse in parallel instead of serializing inside one
/// fetch task. The raw timestamp order decides the split boundary: the largest
/// timestamp strictly below the page's final row is the last value for which
/// the page proves every existing row has been fetched.
fn parse_page_rows<T: Timestamped>(
    rows: &[parse::RawRow<'_>],
    parser: Parser<T>,
    window: Option<SliceWindow>,
    trim_trailing_ties: bool,
    symbol: &str,
) -> Result<ParsedPage<T>, ProviderError> {
    let mut last_nanos: Option<i64> = None;
    let mut safe_split_nanos: Option<i64> = None;
    for row in rows {
        if let Some(nanos) = row.nanos() {
            if last_nanos.is_some_and(|last| nanos != last) {
                safe_split_nanos = last_nanos;
            }
            last_nanos = Some(nanos);
        }
    }
    let trim_below = if trim_trailing_ties {
        safe_split_nanos
    } else {
        None
    };
    let mut records = Vec::with_capacity(rows.len());
    for row in rows {
        if let Some(boundary) = trim_below {
            let nanos = row.nanos();
            if nanos.is_some_and(|nanos| nanos > boundary) {
                continue;
            }
        }
        let record = parser(row, symbol)?;
        let keep = match window {
            Some(window) => window.contains(record.timestamp()),
            None => true,
        };
        if keep {
            records.push(record);
        }
    }
    Ok(ParsedPage {
        records,
        safe_split_nanos,
    })
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

/// The provider timeframe token one lake interval requests.
fn timeframe_value(interval: BarInterval) -> &'static str {
    match interval {
        BarInterval::OneMinute => "1Min",
        BarInterval::OneDay => "1Day",
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

/// Deduplicate option contracts by OSI symbol, first occurrence winning.
///
/// Mirrors `_normalize_contracts`: the first record for a symbol decides both
/// the request order and the contract metadata used to normalize its rows.
fn normalize_contracts(contracts: &[ContractRecord]) -> Vec<(String, ContractRecord)> {
    let mut seen: HashSet<String> = HashSet::new();
    let mut result = Vec::with_capacity(contracts.len());
    for contract in contracts {
        if seen.insert(contract.symbol.clone()) {
            result.push((contract.symbol.clone(), contract.clone()));
        }
    }
    result
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

fn resource_error(error: lake_contracts::DataError) -> ProviderError {
    ProviderError::Configuration(error.to_string())
}
fn append_records<T: RecordMemory>(
    records: &mut Vec<T>,
    incoming: Vec<T>,
    bytes: &mut usize,
) -> Result<(), ProviderError> {
    *bytes = bytes.saturating_add(
        incoming
            .iter()
            .map(RecordMemory::allocated_bytes)
            .sum::<usize>(),
    );
    lake_pipeline::Pipeline::global()
        .check_dataset_bytes(*bytes)
        .map_err(resource_error)?;
    records.extend(incoming);
    Ok(())
}
async fn sort_records<T: Timestamped + Send + 'static>(
    mut records: Vec<T>,
) -> Result<Vec<T>, ProviderError> {
    let pipeline = lake_pipeline::Pipeline::global();
    let cpu = pipeline.cpu().await.map_err(resource_error)?;
    tokio::task::spawn_blocking(move || {
        let _cpu = cpu;
        let mut timer = pipeline.timer(lake_pipeline::Stage::Sort);
        timer.rows(records.len());
        records.sort_by_key(Timestamped::timestamp);
        records
    })
    .await
    .map_err(|e| ProviderError::Protocol(e.to_string()))
}
