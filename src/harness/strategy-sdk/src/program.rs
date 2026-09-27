//! Authoring traits and the capability surfaces they may touch.
//!
//! The capability split mirrors the Python contract exactly: a
//! [`StudyProgram`] never sees the order surface — its only context
//! interaction is publishing metrics through [`MetricPublisher`] — while a
//! [`StrategyProgram`] submits orders through [`OrderSink`], whose host
//! stamps the event time on every intent exactly as the Python adapter does.

use num::PyDecimal;

use crate::abi::{
    CDecimal, CMarketOrder, CMetricSink, COrderSink, CStr, METRIC_INVALID_NAME, METRIC_NOT_FINITE,
    METRIC_OK, METRIC_RESERVED_NAME, SDK_OK,
};
use crate::events::{Fill, ProgramStart, QuoteEvent, TradeEvent};

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
/// Constructed once per run with the configured JSON parameters; the market
/// callbacks fold events in replay order and `on_stop` publishes the run's
/// custom metrics.
pub trait StudyProgram {
    /// Construct the program from the configured JSON parameters.
    fn new(parameters: serde_json::Value) -> Self
    where
        Self: Sized;

    /// Fold one top-of-book quote.
    fn on_quote(&mut self, _event: &QuoteEvent) {}

    /// Fold one trade print.
    fn on_trade(&mut self, _event: &TradeEvent) {}

    /// Publish final metrics once after the replay.
    fn on_stop(&mut self, _metrics: &mut MetricPublisher) {}
}

/// A strategy program: the full runtime surface including order submission.
pub trait StrategyProgram {
    /// Construct the program from the configured JSON parameters.
    fn new(parameters: serde_json::Value) -> Self
    where
        Self: Sized;

    /// Run once before the first market event.
    fn on_start(&mut self, _start: &ProgramStart) {}

    /// Fold one top-of-book quote and submit orders through the sink.
    fn on_quote(&mut self, _event: &QuoteEvent, _orders: &mut OrderSink) {}

    /// Fold one trade print and submit orders through the sink.
    fn on_trade(&mut self, _event: &TradeEvent, _orders: &mut OrderSink) {}

    /// Fold one booked fill owned by this program.
    fn on_fill(&mut self, _fill: &Fill) {}

    /// Publish final metrics once after the replay.
    fn on_stop(&mut self, _metrics: &mut MetricPublisher) {}
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
    pub quantity: PyDecimal,
    /// Order type.
    pub order_type: OrderType,
    /// Time in force.
    pub time_in_force: TimeInForce,
    /// Limit price, required for limit and stop-limit orders.
    pub limit_price: Option<PyDecimal>,
    /// Stop price, required for stop and stop-limit orders.
    pub stop_price: Option<PyDecimal>,
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

fn optional_decimal(value: Option<PyDecimal>) -> (bool, CDecimal) {
    match value {
        Some(value) => (true, CDecimal::from_decimal(value)),
        None => (false, CDecimal::from_decimal(PyDecimal::from_i128(0))),
    }
}
