//! The screening data-source port and its market-lake adapter.
//!
//! The harness never reads lake files: [`ScreeningDataSource`] is the injected
//! boundary that serves the as-of universe, its bars, and its fact sheets.
//! [`LakeScreeningSource`] is the real adapter. It reads the frozen market-lake
//! HTTP routes through `harness-lakeclient`: the reference routes for the
//! as-of universe and facts, and bars ensure plus a streaming Arrow IPC read
//! for the lookback window. There is no synthetic-data fallback by
//! construction: every lake failure fails the run loudly.

use std::collections::BTreeMap;
use std::sync::Arc;

use arrow_array::cast::AsArray;
use arrow_array::types::ArrowPrimitiveType;
use arrow_array::types::Decimal128Type;
use arrow_array::types::DecimalType;
use arrow_array::types::TimestampNanosecondType;
use arrow_array::Array;
use arrow_array::RecordBatch;
use chrono::DateTime;
use chrono::Days;
use chrono::NaiveDate;
use chrono::Utc;
use harness_lakeclient::BlockingLakeClient;
use harness_lakeclient::EnsureSpec;
use harness_lakeclient::LakeClientError;
use harness_lakeclient::ReadSpec;
use harness_strategy_sdk::Bar;

/// A screening data-source failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ScreeningDataSourceError {
    /// The source could not serve the requested universe or window.
    Data(String),
}

impl ScreeningDataSourceError {
    /// Construct a data-plane error.
    #[must_use]
    pub fn data(detail: impl Into<String>) -> Self {
        Self::Data(detail.into())
    }
}

impl std::fmt::Display for ScreeningDataSourceError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Data(detail) => write!(formatter, "screening data source failed: {detail}"),
        }
    }
}

impl std::error::Error for ScreeningDataSourceError {}

/// The data boundary a screening run reads through.
///
/// Implementations return exact values (no floats, no synthetic rows) or a
/// typed error; an error fails the run. All three methods are called with the
/// host's canonical as-of date (`YYYY-MM-DD`).
pub trait ScreeningDataSource: Send + Sync {
    /// The as-of universe symbols.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the universe cannot be resolved.
    fn universe(&self, as_of: &str) -> Result<Vec<String>, ScreeningDataSourceError>;

    /// Daily or minute bars per symbol for the lookback window ending at
    /// `as_of`; a symbol without bars is present with an empty list.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the window cannot be served.
    fn bars(
        &self,
        symbols: &[String],
        interval: &str,
        as_of: &str,
        lookback_days: u32,
    ) -> Result<BTreeMap<String, Vec<Bar>>, ScreeningDataSourceError>;

    /// Engine fact-sheet entries per symbol as of `as_of`.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the facts cannot be resolved.
    fn facts(
        &self,
        symbols: &[String],
        as_of: &str,
    ) -> Result<BTreeMap<String, BTreeMap<String, String>>, ScreeningDataSourceError>;
}

/// The blocking lake reads the screening adapter needs.
///
/// [`BlockingLakeClient`] implements this over the async market-lake client;
/// tests inject a deterministic fake.
pub trait ScreeningLakeReads: Send + Sync {
    /// Ensure one symbol/data-type window, polling to completion.
    ///
    /// # Errors
    ///
    /// Returns a typed lake error under the same conditions as
    /// [`BlockingLakeClient::ensure`].
    fn ensure(&self, spec: &EnsureSpec) -> Result<(), LakeClientError>;

    /// Read one ensured dataset window as decoded Arrow IPC batches.
    ///
    /// # Errors
    ///
    /// Returns a typed lake error under the same conditions as
    /// [`BlockingLakeClient::read_arrow`].
    fn read_arrow(&self, spec: &ReadSpec) -> Result<Vec<RecordBatch>, LakeClientError>;

    /// Return the as-of universe symbols.
    ///
    /// # Errors
    ///
    /// Returns a typed lake error under the same conditions as
    /// [`BlockingLakeClient::reference_universe`].
    fn reference_universe(&self, as_of: &str) -> Result<Vec<String>, LakeClientError>;

    /// Return the engine fact-sheet entries per symbol as of `as_of`.
    ///
    /// # Errors
    ///
    /// Returns a typed lake error under the same conditions as
    /// [`BlockingLakeClient::reference_facts`].
    fn reference_facts(
        &self,
        symbols: &[String],
        as_of: &str,
    ) -> Result<BTreeMap<String, BTreeMap<String, String>>, LakeClientError>;
}

impl ScreeningLakeReads for BlockingLakeClient {
    fn ensure(&self, spec: &EnsureSpec) -> Result<(), LakeClientError> {
        BlockingLakeClient::ensure(self, spec)
    }

    fn read_arrow(&self, spec: &ReadSpec) -> Result<Vec<RecordBatch>, LakeClientError> {
        BlockingLakeClient::read_arrow(self, spec)
    }

    fn reference_universe(&self, as_of: &str) -> Result<Vec<String>, LakeClientError> {
        BlockingLakeClient::reference_universe(self, as_of)
    }

    fn reference_facts(
        &self,
        symbols: &[String],
        as_of: &str,
    ) -> Result<BTreeMap<String, BTreeMap<String, String>>, LakeClientError> {
        BlockingLakeClient::reference_facts(self, symbols, as_of)
    }
}

/// The market-lake screening data source.
///
/// `universe` and `facts` read the reference routes. `bars` fans out over the
/// requested equity symbols: each symbol is ensured for `data_type=bars` with
/// the interval (the lake ensures trades internally) and then read back as
/// Arrow IPC. Rows map into exact [`Bar`] strings; a symbol the lake has no
/// bars for maps to an empty list.
pub struct LakeScreeningSource {
    client: Arc<dyn ScreeningLakeReads>,
}

impl std::fmt::Debug for LakeScreeningSource {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("LakeScreeningSource")
            .finish_non_exhaustive()
    }
}

impl LakeScreeningSource {
    /// Compose the adapter over an injected lake-read boundary.
    #[must_use]
    pub fn new(client: Arc<dyn ScreeningLakeReads>) -> Self {
        Self { client }
    }

    /// Compose the adapter over the client resolved from `LAKE_API_URL` /
    /// `LAKE_API_BASE_URL` and `LAKE_API__BEARER_TOKEN`.
    ///
    /// # Errors
    ///
    /// Returns [`LakeClientError::Config`] when the environment has no bearer
    /// token, so a misconfigured deployment fails loudly before any run.
    pub fn from_env() -> Result<Self, LakeClientError> {
        Ok(Self::new(Arc::new(BlockingLakeClient::from_env()?)))
    }
}

impl ScreeningDataSource for LakeScreeningSource {
    fn universe(&self, as_of: &str) -> Result<Vec<String>, ScreeningDataSourceError> {
        self.client
            .reference_universe(as_of)
            .map_err(ScreeningDataSourceError::from)
    }

    fn bars(
        &self,
        symbols: &[String],
        interval: &str,
        as_of: &str,
        lookback_days: u32,
    ) -> Result<BTreeMap<String, Vec<Bar>>, ScreeningDataSourceError> {
        let start_date = window_start(as_of, lookback_days)?;
        let mut bars = BTreeMap::new();
        for symbol in symbols {
            let ensure =
                EnsureSpec::new("equity", "bars", symbol, &start_date, as_of).with_interval("1min");
            self.client
                .ensure(&ensure)
                .map_err(ScreeningDataSourceError::from)?;
            let read =
                ReadSpec::new("equity", "bars", symbol, &start_date, as_of).with_interval("1min");
            let batches = self
                .client
                .read_arrow(&read)
                .map_err(ScreeningDataSourceError::from)?;
            let mut records = Vec::new();
            for batch in &batches {
                records.extend(bars_from_batch(batch)?);
            }
            let records = aggregate_screen_bars(symbol, interval, as_of, records)?;
            bars.insert(symbol.clone(), records);
        }
        Ok(bars)
    }

    fn facts(
        &self,
        symbols: &[String],
        as_of: &str,
    ) -> Result<BTreeMap<String, BTreeMap<String, String>>, ScreeningDataSourceError> {
        self.client
            .reference_facts(symbols, as_of)
            .map_err(ScreeningDataSourceError::from)
    }
}

/// Derive every screener cadence from its single acquired minute series.
pub(crate) fn aggregate_screen_bars(
    symbol: &str,
    interval: &str,
    as_of: &str,
    records: Vec<Bar>,
) -> Result<Vec<Bar>, ScreeningDataSourceError> {
    let parse = |value: &str| {
        value
            .parse::<num::Decimal>()
            .map_err(|e| ScreeningDataSourceError::data(e.to_string()))
    };
    let inputs = records
        .into_iter()
        .enumerate()
        .map(|(index, bar)| {
            Ok(harness_trading::program::EngineBar {
                event_id: format!("screen:{symbol}:{index}"),
                timestamp_ns: bar.interval_start_ns,
                sequence: index as u64,
                symbol: symbol.to_string(),
                open: parse(&bar.open)?,
                high: parse(&bar.high)?,
                low: parse(&bar.low)?,
                close: parse(&bar.close)?,
                volume: parse(&bar.volume)?,
            })
        })
        .collect::<Result<Vec<_>, ScreeningDataSourceError>>()?;
    let mut config =
        backtest::backtest::config::BacktestConfig::new("screen-context", 0, i64::MAX, 0)
            .map_err(|e| ScreeningDataSourceError::data(e.to_string()))?;
    config.symbols = vec![symbol.to_string()];
    config.data_bar_interval = interval.into();
    config.data_source_bar_interval = "1min".into();
    config.start_ns = i64::MIN;
    config.end_ns = NaiveDate::parse_from_str(as_of, "%Y-%m-%d")
        .ok()
        .and_then(|date| date.succ_opt())
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .and_then(|stamp| stamp.and_utc().timestamp_nanos_opt())
        .ok_or_else(|| ScreeningDataSourceError::data("as-of date exceeds timestamp bounds"))?;
    let frames = backtest::backtest::subscriptions::assemble_bar_observations(&config, inputs)
        .map_err(|e| ScreeningDataSourceError::data(e.to_string()))?;
    let records = frames
        .into_iter()
        .filter_map(|frame| {
            frame.bars_by_symbol.get(symbol).map(|bar| Bar {
                date: DateTime::<Utc>::from_timestamp_nanos(frame.interval_start_ns)
                    .date_naive()
                    .to_string(),
                interval_start_ns: frame.interval_start_ns,
                interval_end_ns: frame.interval_end_ns,
                available_at_ns: frame.available_at_ns,
                open: bar.open.to_string(),
                high: bar.high.to_string(),
                low: bar.low.to_string(),
                close: bar.close.to_string(),
                volume: bar.volume.to_string(),
            })
        })
        .collect();
    Ok(records)
}

impl From<LakeClientError> for ScreeningDataSourceError {
    fn from(error: LakeClientError) -> Self {
        Self::Data(error.to_string())
    }
}

/// The first session of the lookback window (`as_of - lookback_days`).
fn window_start(as_of: &str, lookback_days: u32) -> Result<String, ScreeningDataSourceError> {
    let date = NaiveDate::parse_from_str(as_of, "%Y-%m-%d").map_err(|error| {
        ScreeningDataSourceError::data(format!("invalid as-of date {as_of:?}: {error}"))
    })?;
    let start = date
        .checked_sub_days(Days::new(u64::from(lookback_days)))
        .ok_or_else(|| {
            ScreeningDataSourceError::data(format!(
                "the lookback window underflows the calendar for as-of {as_of:?}"
            ))
        })?;
    Ok(start.format("%Y-%m-%d").to_string())
}

/// Map every row of one `equity.bars` batch into exact [`Bar`] records.
///
/// `timestamp` renders as the UTC session date; `open`, `high`, `low`,
/// `close`, and `volume` render through the Arrow decimal formatter, so the
/// exact `decimal128(38, 18)` value crosses with its full scale and no float
/// roundtrip.
fn bars_from_batch(batch: &RecordBatch) -> Result<Vec<Bar>, ScreeningDataSourceError> {
    let timestamps = primitive_column::<TimestampNanosecondType>(batch, "timestamp")?;
    let open = decimal_column(batch, "open")?;
    let high = decimal_column(batch, "high")?;
    let low = decimal_column(batch, "low")?;
    let close = decimal_column(batch, "close")?;
    let volume = decimal_column(batch, "volume")?;

    let mut bars = Vec::with_capacity(batch.num_rows());
    for row in 0..batch.num_rows() {
        let date = DateTime::<Utc>::from_timestamp_nanos(timestamps.value(row))
            .date_naive()
            .format("%Y-%m-%d")
            .to_string();
        bars.push(Bar {
            date,
            interval_start_ns: timestamps.value(row),
            interval_end_ns: timestamps.value(row) + 60_000_000_000,
            available_at_ns: timestamps.value(row) + 60_000_000_000,
            open: decimal_text(open, row),
            high: decimal_text(high, row),
            low: decimal_text(low, row),
            close: decimal_text(close, row),
            volume: decimal_text(volume, row),
        });
    }
    Ok(bars)
}

/// Render one `decimal128` value through the Arrow decimal formatter.
fn decimal_text(array: &arrow_array::Decimal128Array, row: usize) -> String {
    Decimal128Type::format_decimal(array.value(row), array.precision(), array.scale())
}

/// Borrow one required primitive column from a batch.
fn primitive_column<'a, T: ArrowPrimitiveType>(
    batch: &'a RecordBatch,
    name: &str,
) -> Result<&'a arrow_array::PrimitiveArray<T>, ScreeningDataSourceError> {
    let column = batch.column_by_name(name).ok_or_else(|| {
        ScreeningDataSourceError::data(format!("the lake bars stream has no {name:?} column"))
    })?;
    column.as_primitive_opt::<T>().ok_or_else(|| {
        ScreeningDataSourceError::data(format!(
            "the lake bars stream column {name:?} has the wrong Arrow type"
        ))
    })
}

/// Borrow one required `decimal128(38, 18)` column from a batch.
fn decimal_column<'a>(
    batch: &'a RecordBatch,
    name: &str,
) -> Result<&'a arrow_array::Decimal128Array, ScreeningDataSourceError> {
    let column = batch.column_by_name(name).ok_or_else(|| {
        ScreeningDataSourceError::data(format!("the lake bars stream has no {name:?} column"))
    })?;
    let array = column
        .as_any()
        .downcast_ref::<arrow_array::Decimal128Array>();
    array.ok_or_else(|| {
        ScreeningDataSourceError::data(format!(
            "the lake bars stream column {name:?} is not decimal128(38, 18)"
        ))
    })
}
