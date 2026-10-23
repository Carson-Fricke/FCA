//! Authoring traits and the capability surfaces they may touch.
//!
//! The capability split mirrors the Python contract exactly: a
//! [`StudyProgram`] never sees the order surface and observes market inputs;
//! its only context interaction is publishing evidence through
//! [`MetricPublisher`] — while a [`StrategyProgram`] submits orders through
//! [`OrderSink`], whose host stamps the event time on every intent exactly as
//! the Python adapter does. A [`ScreeningProgram`] is the third kind: one-shot
//! cross-sectional evaluation over a host-built [`ScreenContext`], returning
//! ranked rows and having no order surface, file, network, or clock access by
//! construction.

use std::collections::BTreeMap;

use num::Decimal;
use serde::Deserialize;
use serde::Serialize;

use crate::abi::CDecimal;
use crate::abi::CMarketOrder;
use crate::abi::CMetricSink;
use crate::abi::COrderSink;
use crate::abi::CStr;
use crate::abi::METRIC_INVALID_NAME;
use crate::abi::METRIC_NOT_FINITE;
use crate::abi::METRIC_OK;
use crate::abi::METRIC_RESERVED_NAME;
use crate::abi::SDK_OK;
use crate::events::Fill;
use crate::events::ProgramStart;
use crate::MarketConsumer;
use crate::ObservationContext;

/// The metric names reserved by the harness; publishing them fails.
pub const RESERVED_METRIC_NAMES: &[&str] = &[
    "cagr",
    "fees_total",
    "fill_count",
    "max_drawdown",
    "slippage_total",
    "strategy_id",
    "total_return",
];

/// A study program: fit and observe history, publish metrics, no execution.
///
/// Constructed once per run with configured JSON parameters. Market callbacks
/// live on [`MarketConsumer`]; `on_stop` publishes the run's custom metrics.
pub trait StudyProgram: MarketConsumer<Context = ObservationContext> {
    /// Construct the program from the configured JSON parameters.
    fn new(parameters: serde_json::Value) -> Self
    where
        Self: Sized;

    /// Publish final metrics once after the replay.
    fn on_stop(&mut self, _metrics: &mut MetricPublisher) {}
}

/// A strategy program: the full runtime surface including order submission.
pub trait StrategyProgram: MarketConsumer<Context = OrderSink> {
    /// Construct the program from the configured JSON parameters.
    fn new(parameters: serde_json::Value) -> Self
    where
        Self: Sized;

    /// Run once before the first market event.
    fn on_start(&mut self, _start: &ProgramStart) {}

    /// Fold one booked fill owned by this program. Orders may produce multiple
    /// partial fills: update inventory on every fill and remain open until actual
    /// quantity reaches zero. `building_blocks::FillLedger` provides exact accounting.
    fn on_fill(&mut self, _fill: &Fill) {}

    /// Publish final metrics once after the replay.
    fn on_stop(&mut self, _metrics: &mut MetricPublisher) {}
}

/// A screening program: one-shot cross-sectional evaluation, no execution.
///
/// Constructed once per run with the configured JSON parameters; the host
/// builds exactly one [`ScreenContext`] (universe, bars, facts, and
/// precomputed indicators) and [`screen`](ScreeningProgram::screen) returns
/// the rows the host ranks. The only data surface is the context: a
/// screening program has no order surface, no file, network, or clock access
/// by construction.
pub trait ScreeningProgram {
    /// Construct the program from the configured JSON parameters.
    fn new(parameters: serde_json::Value) -> Self
    where
        Self: Sized;

    /// Evaluate one host-built cross-sectional context and return its rows.
    fn screen(&mut self, ctx: &ScreenContext) -> Vec<ScreenRow>;
}

/// One completed OHLCV bar as the host provides it.
///
/// Every decimal field crosses as the exact decimal string the host built
/// (no float roundtrip); [`volume`](Bar::volume) is exact as well.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bar {
    /// Source window start in Unix nanoseconds.
    pub interval_start_ns: i64,
    /// Source window end in Unix nanoseconds.
    pub interval_end_ns: i64,
    /// Earliest causal availability in Unix nanoseconds.
    pub available_at_ns: i64,
    /// Session date (`YYYY-MM-DD`).
    pub date: String,
    /// Window open.
    pub open: String,
    /// Window high.
    pub high: String,
    /// Window low.
    pub low: String,
    /// Window close.
    pub close: String,
    /// Window volume.
    pub volume: String,
}

/// The one data surface a screening program may read.
///
/// The host builds one context per run: the as-of date, the universe
/// symbols, their bars, their engine fact sheets, and the indicator values
/// precomputed by the harness under their canonical `ind:<name>(<params>)`
/// keys. Maps are [`BTreeMap`]s, so iteration and serialization order are
/// deterministic.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScreenContext {
    /// The as-of date (`YYYY-MM-DD`) the context was built for.
    pub as_of: String,
    /// The as-of universe symbols.
    pub universe: Vec<String>,
    /// Completed bars by interval then symbol; absent inputs stay absent.
    pub bars: BTreeMap<String, BTreeMap<String, Vec<Bar>>>,
    /// Engine fact-sheet entries per symbol.
    pub facts: BTreeMap<String, BTreeMap<String, String>>,
    /// Precomputed indicator values per symbol, keyed by metric key.
    pub indicators: BTreeMap<String, BTreeMap<String, String>>,
}

impl ScreenContext {
    /// The as-of universe symbols.
    #[must_use]
    pub fn universe(&self) -> &[String] {
        &self.universe
    }

    /// The symbol's bars at the requested interval, or an empty slice.
    #[must_use]
    pub fn bars(&self, symbol: &str, interval: &str) -> &[Bar] {
        self.bars
            .get(interval)
            .and_then(|series| series.get(symbol))
            .map_or(&[], Vec::as_slice)
    }

    /// The symbol's fact-sheet entries, or an empty map when it has none.
    #[must_use]
    pub fn facts(&self, symbol: &str) -> &BTreeMap<String, String> {
        static EMPTY: BTreeMap<String, String> = BTreeMap::new();
        self.facts.get(symbol).unwrap_or(&EMPTY)
    }

    /// The symbol's precomputed indicator values, or an empty map when it has
    /// none.
    #[must_use]
    pub fn indicators(&self, symbol: &str) -> &BTreeMap<String, String> {
        static EMPTY: BTreeMap<String, String> = BTreeMap::new();
        self.indicators.get(symbol).unwrap_or(&EMPTY)
    }
}

/// One screening result row.
///
/// Metrics are exact decimal strings where the value is decimal; the host
/// ranks on the caller-named metric with exact decimal comparison, so
/// authored programs never round.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScreenRow {
    /// Instrument symbol.
    pub symbol: String,
    /// Row metrics by name; the rank metric is whichever the host names.
    pub metrics: BTreeMap<String, String>,
}

/// Why a metric publish was rejected.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PublishError {
    /// The name does not match `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$`.
    InvalidName,
    /// The name is reserved by the harness.
    ReservedName,
    /// The value is not finite.
    NotFinite,
    /// The host sink rejected the publish.
    Rejected,
}

impl std::fmt::Display for PublishError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidName => {
                write!(
                    formatter,
                    "metric name must match ^[A-Za-z0-9][A-Za-z0-9._:@/-]{{0,127}}$"
                )
            }
            Self::ReservedName => write!(formatter, "metric name is reserved by the harness"),
            Self::NotFinite => write!(formatter, "metric value must be finite"),
            Self::Rejected => write!(formatter, "metric publish rejected by the host"),
        }
    }
}

impl std::error::Error for PublishError {}

/// The only context capability a study program may use.
///
/// Every publish is validated by the host sink and every rejection is
/// recorded there, so an ignored [`PublishError`] still fails the run.
pub struct MetricPublisher {
    sink: CMetricSink,
}

impl MetricPublisher {
    /// Wrap one host sink.
    ///
    /// # Safety
    ///
    /// `sink.ctx` must point to host memory that outlives the callback and
    /// `sink.publish` must be a valid host function.
    #[must_use]
    pub const unsafe fn from_sink(sink: CMetricSink) -> Self {
        Self { sink }
    }

    /// Publish one finite numeric run metric under one validated name.
    ///
    /// # Errors
    ///
    /// Returns [`PublishError`] when the name is invalid or reserved, the
    /// value is not finite, or the host rejects the publish.
    pub fn publish(&mut self, name: &str, value: f64) -> Result<(), PublishError> {
        let code = unsafe { (self.sink.publish)(self.sink.ctx, CStr::from_slice(name), value) };
        PublishError::from_code(code).map_or(Ok(()), Err)
    }

    /// Publish a sequence, event collection, or typed table for job inspection.
    ///
    /// # Errors
    /// Returns an error if serialization or host validation rejects the output.
    pub fn publish_output(
        &mut self,
        name: &str,
        output: &crate::StudyOutput,
    ) -> Result<(), PublishError> {
        let json = serde_json::to_string(output).map_err(|_| PublishError::Rejected)?;
        let code = unsafe {
            (self.sink.publish_output)(
                self.sink.ctx,
                CStr::from_slice(name),
                CStr::from_slice(&json),
            )
        };
        PublishError::from_code(code).map_or(Ok(()), Err)
    }
}

impl PublishError {
    /// Map a sink result code to its error, or `None` for success.
    #[must_use]
    pub const fn from_code(code: i32) -> Option<Self> {
        match code {
            METRIC_OK => None,
            METRIC_INVALID_NAME => Some(Self::InvalidName),
            METRIC_RESERVED_NAME => Some(Self::ReservedName),
            METRIC_NOT_FINITE => Some(Self::NotFinite),
            _ => Some(Self::Rejected),
        }
    }
}

/// An order intent as the author submits it.
///
/// The event time is not part of the author surface: the host stamps it from
/// the driving event exactly as the Python adapter does.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SubmittedOrder {
    /// Order identifier.
    pub order_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// Order side.
    pub side: Side,
    /// Order quantity.
    pub quantity: Decimal,
    /// Order type.
    pub order_type: OrderType,
    /// Time in force.
    pub time_in_force: TimeInForce,
    /// Limit price, required for limit and stop-limit orders.
    pub limit_price: Option<Decimal>,
    /// Stop price, required for stop and stop-limit orders.
    pub stop_price: Option<Decimal>,
    /// Order sequence.
    pub sequence: u64,
    /// Order event identifier.
    pub event_id: String,
}

/// Why an order submission was rejected.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SubmitError {
    /// The host sink rejected the submission.
    Rejected,
}

impl std::fmt::Display for SubmitError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Rejected => write!(formatter, "order submission rejected by the host"),
        }
    }
}

impl std::error::Error for SubmitError {}

/// The strategy order surface: every submitted intent goes to the host,
/// which records ownership exactly as the Python `orders` handle does.
pub struct OrderSink {
    sink: COrderSink,
}

impl OrderSink {
    /// Wrap one host order sink.
    ///
    /// # Safety
    ///
    /// `sink.ctx` must point to host memory that outlives the callback and
    /// `sink.submit` must be a valid host function.
    #[must_use]
    pub const unsafe fn from_sink(sink: COrderSink) -> Self {
        Self { sink }
    }

    /// Record one submitted order without synchronously producing an outcome.
    ///
    /// # Errors
    ///
    /// Returns [`SubmitError`] when the host rejects the intent.
    pub fn submit(&mut self, order: SubmittedOrder) -> Result<(), SubmitError> {
        let (has_limit, limit) = optional_decimal(order.limit_price);
        let (has_stop, stop) = optional_decimal(order.stop_price);
        let intent = CMarketOrder {
            order_id: CStr::from_slice(&order.order_id),
            symbol: CStr::from_slice(&order.symbol),
            side: CStr::from_slice(order.side.as_str()),
            quantity: CDecimal::from_decimal(order.quantity),
            order_type: CStr::from_slice(order.order_type.as_str()),
            time_in_force: CStr::from_slice(order.time_in_force.as_str()),
            has_limit_price: has_limit,
            limit_price: limit,
            has_stop_price: has_stop,
            stop_price: stop,
            sequence: order.sequence,
            event_id: CStr::from_slice(&order.event_id),
        };
        let code = unsafe { (self.sink.submit)(self.sink.ctx, &intent) };
        if code == SDK_OK {
            Ok(())
        } else {
            Err(SubmitError::Rejected)
        }
    }
}

/// An order side.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    /// Buy.
    Buy,
    /// Sell.
    Sell,
}

impl Side {
    /// The lowercase engine value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Buy => "buy",
            Self::Sell => "sell",
        }
    }
}

/// An order type.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OrderType {
    /// Market.
    Market,
    /// Limit.
    Limit,
    /// Stop.
    Stop,
    /// Stop-limit.
    StopLimit,
}

impl OrderType {
    /// The lowercase engine value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Market => "market",
            Self::Limit => "limit",
            Self::Stop => "stop",
            Self::StopLimit => "stop_limit",
        }
    }
}

/// A time-in-force policy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TimeInForce {
    /// Day.
    Day,
    /// Good until cancelled.
    Gtc,
    /// Immediate or cancel.
    Ioc,
}

impl TimeInForce {
    /// The lowercase engine value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Day => "day",
            Self::Gtc => "gtc",
            Self::Ioc => "ioc",
        }
    }
}

fn optional_decimal(value: Option<Decimal>) -> (bool, CDecimal) {
    match value {
        Some(value) => (true, CDecimal::from_decimal(value)),
        None => (false, CDecimal::from_decimal(Decimal::from_i128(0))),
    }
}
