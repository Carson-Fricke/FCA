//! The versioned C ABI between the host and a JIT-compiled authored artifact.
//!
//! Rust trait objects never cross the `extern "C"` boundary. Every crossing
//! is a plain extern function over the `#[repr(C)]` structs in this module,
//! so the ABI evolves explicitly: argument structs only grow at the tail with
//! an ABI-version bump, and the loader rejects artifacts whose
//! [`SDK_ABI_VERSION`] differs from the host's.
//!
//! Decimals cross as [`CDecimal`], an exact mirror of the engine's
//! variable-scale decimal (sign, `u128` coefficient, `i32` scale); strings
//! cross as borrowed [`CStr`] views valid for the duration of one callback.

use std::os::raw::c_void;

use num::Decimal;

/// The ABI version the host and every artifact must agree on.
///
/// Version 5 adds structured exploratory output publication and multi-timeframe
/// screening contexts. Artifacts must be rebuilt against the matching SDK.
pub const SDK_ABI_VERSION: u32 = 5;

/// Callback result: the author code completed.
pub const SDK_OK: i32 = 0;

/// Callback result: the author code failed (error or caught panic).
pub const SDK_ERROR: i32 = 1;

/// Metric publish accepted.
pub const METRIC_OK: i32 = 0;

/// Metric publish rejected: invalid metric name.
pub const METRIC_INVALID_NAME: i32 = 1;

/// Metric publish rejected: reserved metric name.
pub const METRIC_RESERVED_NAME: i32 = 2;

/// Metric publish rejected: non-finite value.
pub const METRIC_NOT_FINITE: i32 = 3;

/// Metric publish rejected for another reason.
pub const METRIC_REJECTED: i32 = 4;

/// A borrowed UTF-8 string view valid for the duration of one callback.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CStr {
    /// Pointer to the first UTF-8 byte; never null when produced by either
    /// side, including for empty strings.
    pub ptr: *const u8,
    /// Length in bytes.
    pub len: usize,
}

impl CStr {
    /// Build one borrowed view over a Rust string slice.
    #[must_use]
    pub fn from_slice(value: &str) -> Self {
        Self {
            ptr: value.as_ptr(),
            len: value.len(),
        }
    }

    /// Borrow the view as a Rust string slice.
    ///
    /// # Safety
    ///
    /// `ptr` must point to `len` valid UTF-8 bytes that remain readable for
    /// the returned slice's lifetime.
    #[must_use]
    pub unsafe fn as_str<'a>(self) -> Option<&'a str> {
        if self.ptr.is_null()
            || self.len > isize::MAX as usize
            || (self.ptr as usize).checked_add(self.len).is_none()
        {
            return None;
        }
        let bytes = std::slice::from_raw_parts(self.ptr, self.len);
        std::str::from_utf8(bytes).ok()
    }
}

/// The exact decimal crossing the ABI: sign, magnitude coefficient, scale.
///
/// The value is `(-1)^negative * coefficient * 10^-scale`; conversion to and
/// from the engine decimal is lossless, including negative zero and preserved
/// trailing zeros.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CDecimal {
    /// Sign flag (Python `is_signed`), set for `-0` as well.
    pub negative: bool,
    /// Magnitude coefficient.
    pub coefficient: u128,
    /// Decimal scale; the exponent is `-scale`.
    pub scale: i32,
}

impl CDecimal {
    /// Lossless conversion from the engine decimal.
    #[must_use]
    pub const fn from_decimal(value: Decimal) -> Self {
        Self {
            negative: value.is_signed(),
            coefficient: value.coefficient(),
            scale: value.scale(),
        }
    }

    /// Lossless reconstruction of the engine decimal.
    #[must_use]
    pub const fn to_decimal(self) -> Decimal {
        Decimal::from_parts(self.negative, self.coefficient, self.scale)
    }
}

/// A top-of-book quote crossing the ABI.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CQuoteEvent {
    /// Event identifier.
    pub event_id: CStr,
    /// Event time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Global sequence.
    pub sequence: u64,
    /// Instrument symbol.
    pub symbol: CStr,
    /// Best bid.
    pub bid: CDecimal,
    /// Best ask.
    pub ask: CDecimal,
    /// Bid size.
    pub bid_quantity: CDecimal,
    /// Ask size.
    pub ask_quantity: CDecimal,
    /// Reporting venue.
    pub venue: CStr,
}

/// A trade print crossing the ABI.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CTradeEvent {
    /// Event identifier.
    pub event_id: CStr,
    /// Event time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Global sequence.
    pub sequence: u64,
    /// Instrument symbol.
    pub symbol: CStr,
    /// Print price.
    pub price: CDecimal,
    /// Print quantity.
    pub quantity: CDecimal,
    /// Reporting venue.
    pub venue: CStr,
    /// Trade identifier.
    pub trade_id: CStr,
}

/// One OHLCV bar crossing the ABI.
///
/// Atomic frames contain these bars for every market consumer.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CBarEvent {
    /// Event identifier.
    pub event_id: CStr,
    /// Event time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Global sequence.
    pub sequence: u64,
    /// Instrument symbol.
    pub symbol: CStr,
    /// Interval open.
    pub open: CDecimal,
    /// Interval high.
    pub high: CDecimal,
    /// Interval low.
    pub low: CDecimal,
    /// Interval close.
    pub close: CDecimal,
    /// Interval volume.
    pub volume: CDecimal,
}

/// One atomic interval crossing the ABI as borrowed arrays, never Rust maps.
/// Array storage and string views remain readable for the entire callback.
/// A null array pointer is permitted only when its count is zero.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CBarFrame {
    /// Inclusive interval start in epoch nanoseconds.
    pub interval_start_ns: i64,
    /// Exclusive interval end in epoch nanoseconds; strictly after start.
    pub interval_end_ns: i64,
    /// Availability time, at or after interval end.
    pub available_at_ns: i64,
    /// Bars with unique symbols and timestamps equal to interval start.
    pub bars: *const CBarEvent,
    /// Number of bars.
    pub bars_len: usize,
    /// Unique missing symbols, disjoint from the bars.
    pub missing_symbols: *const CStr,
    /// Number of missing symbols.
    pub missing_symbols_len: usize,
    /// Canonical interval identity: positive minute/hour cadence or one session day.
    pub interval: CStr,
}

/// One deterministic fill crossing the ABI.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CFill {
    /// Fill identifier (also the report event identifier).
    pub event_id: CStr,
    /// Fill time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Fill sequence.
    pub sequence: u64,
    /// Fill identifier.
    pub fill_id: CStr,
    /// Order identifier.
    pub order_id: CStr,
    /// Instrument symbol.
    pub symbol: CStr,
    /// `buy` or `sell`.
    pub side: CStr,
    /// Fill quantity.
    pub quantity: CDecimal,
    /// Fill price.
    pub price: CDecimal,
    /// Fill fee.
    pub fee: CDecimal,
    /// Liquidity role.
    pub liquidity: CStr,
}

/// Startup facts handed to a strategy program crossing the ABI.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CStart {
    /// Instance identifier.
    pub instance_id: CStr,
    /// Shared account identifier.
    pub account_id: CStr,
    /// Strategy universe symbols.
    pub symbols: CSlice,
    /// Run seed.
    pub seed: u64,
    /// Initial book cash.
    pub initial_cash: CDecimal,
    /// Run start in epoch nanoseconds.
    pub start_ns: i64,
}

/// A borrowed array of string views.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CSlice {
    /// Pointer to the first element.
    pub ptr: *const CStr,
    /// Element count.
    pub len: usize,
}

/// One order intent submitted by a strategy program crossing the ABI.
///
/// The host stamps `timestamp_ns` from the driving event, exactly as the
/// Python adapter does; the author never supplies it.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CMarketOrder {
    /// Order identifier.
    pub order_id: CStr,
    /// Instrument symbol.
    pub symbol: CStr,
    /// `buy` or `sell`.
    pub side: CStr,
    /// Order quantity.
    pub quantity: CDecimal,
    /// Order type (`market`, `limit`, `stop`, `stop_limit`).
    pub order_type: CStr,
    /// Time in force (`day`, `gtc`, `ioc`).
    pub time_in_force: CStr,
    /// Whether `limit_price` carries a value.
    pub has_limit_price: bool,
    /// Limit price when `has_limit_price` is set.
    pub limit_price: CDecimal,
    /// Whether `stop_price` carries a value.
    pub has_stop_price: bool,
    /// Stop price when `has_stop_price` is set.
    pub stop_price: CDecimal,
    /// Order sequence.
    pub sequence: u64,
    /// Order event identifier.
    pub event_id: CStr,
}

/// The host-owned metric sink handed to `on_stop`.
///
/// The sink validates metric names and finiteness and records every
/// rejection, so a rejected metric fails the run even when the author ignores
/// the returned error.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CMetricSink {
    /// Host context pointer.
    pub ctx: *mut c_void,
    /// Publish callback.
    pub publish: MetricPublishFn,
    /// Publish a typed exploratory output as validated JSON.
    pub publish_output: OutputPublishFn,
}

/// Publish one finite metric value under one validated name.
pub type MetricPublishFn = unsafe extern "C" fn(ctx: *mut c_void, name: CStr, value: f64) -> i32;

/// Publish one structured output; the host copies JSON during the callback.
pub type OutputPublishFn = unsafe extern "C" fn(ctx: *mut c_void, name: CStr, json: CStr) -> i32;

/// The host-owned order sink handed to strategy callbacks.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct COrderSink {
    /// Host context pointer.
    pub ctx: *mut c_void,
    /// Submit callback.
    pub submit: OrderSubmitFn,
}

/// Submit one order intent to the host.
pub type OrderSubmitFn = unsafe extern "C" fn(ctx: *mut c_void, order: *const CMarketOrder) -> i32;

/// The vtable a study artifact exports.
///
/// A study has no order surface by construction: the vtable carries no
/// order-submit path, and its quote, trade, and frame callbacks return no orders.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct StudyProgramVTable {
    /// ABI version this vtable was built for.
    pub abi_version: u32,
    /// Reserved for tail growth; must stay zero.
    pub reserved: u32,
    /// Construct the program state from a JSON parameters document.
    pub create: StudyCreateFn,
    /// Fold one atomic interval frame.
    pub on_bar_frame: StudyBarFrameFn,
    /// Publish final metrics through the host sink.
    pub on_stop: StudyStopFn,
    /// Destroy the program state.
    pub destroy: StudyDestroyFn,
    /// Observe a quote without execution capability.
    pub on_quote: StudyQuoteFn,
    /// Observe a trade without execution capability.
    pub on_trade: StudyTradeFn,
    /// Borrow effective subscription JSON after construction.
    pub subscriptions: SubscriptionsFn,
}

/// Construct a study program state from a JSON parameters document.
///
/// Returns the state pointer, or null on failure (details via
/// `harness_sdk_last_error`).
pub type StudyCreateFn = unsafe extern "C" fn(parameters: CStr) -> *mut c_void;

/// Fold one atomic interval frame into a study program state.
pub type StudyBarFrameFn = unsafe extern "C" fn(state: *mut c_void, frame: *const CBarFrame) -> i32;
pub type StudyQuoteFn = unsafe extern "C" fn(state: *mut c_void, event: *const CQuoteEvent) -> i32;
pub type StudyTradeFn = unsafe extern "C" fn(state: *mut c_void, event: *const CTradeEvent) -> i32;
/// State-owned JSON array valid until the next subscriptions call or destroy.
pub type SubscriptionsFn = unsafe extern "C" fn(state: *mut c_void) -> CStr;

/// Publish final metrics from a study program state.
pub type StudyStopFn = unsafe extern "C" fn(state: *mut c_void, metrics: *const CMetricSink) -> i32;

/// Destroy a study program state.
pub type StudyDestroyFn = unsafe extern "C" fn(state: *mut c_void);

/// The vtable a strategy artifact exports.
///
/// Market callbacks receive an order sink; every submitted intent is handed
/// to the host, which stamps the event time exactly as the Python adapter
/// does.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct StrategyProgramVTable {
    /// ABI version this vtable was built for.
    pub abi_version: u32,
    /// Reserved for tail growth; must stay zero.
    pub reserved: u32,
    /// Construct the program state from a JSON parameters document.
    pub create: StrategyCreateFn,
    /// Run once before the first market event.
    pub on_start: StrategyStartFn,
    /// Fold one quote and submit orders through the sink.
    pub on_quote: StrategyQuoteFn,
    /// Fold one trade and submit orders through the sink.
    pub on_trade: StrategyTradeFn,
    /// Fold one booked fill.
    pub on_fill: StrategyFillFn,
    /// Publish final metrics through the host sink.
    pub on_stop: StrategyStopFn,
    /// Destroy the program state.
    pub destroy: StrategyDestroyFn,
    /// Fold an atomic bar frame and submit orders.
    pub on_bar_frame: StrategyBarFrameFn,
    /// Borrow effective subscription JSON after construction.
    pub subscriptions: SubscriptionsFn,
}

/// Construct a strategy program state from a JSON parameters document.
pub type StrategyCreateFn = unsafe extern "C" fn(parameters: CStr) -> *mut c_void;
pub type StrategyBarFrameFn = unsafe extern "C" fn(
    state: *mut c_void,
    frame: *const CBarFrame,
    orders: *const COrderSink,
) -> i32;

/// Run one strategy program start.
pub type StrategyStartFn = unsafe extern "C" fn(state: *mut c_void, start: *const CStart) -> i32;

/// Fold one quote into a strategy program state.
pub type StrategyQuoteFn = unsafe extern "C" fn(
    state: *mut c_void,
    event: *const CQuoteEvent,
    orders: *const COrderSink,
) -> i32;

/// Fold one trade into a strategy program state.
pub type StrategyTradeFn = unsafe extern "C" fn(
    state: *mut c_void,
    event: *const CTradeEvent,
    orders: *const COrderSink,
) -> i32;

/// Fold one booked fill into a strategy program state.
pub type StrategyFillFn = unsafe extern "C" fn(state: *mut c_void, fill: *const CFill) -> i32;

/// Publish final metrics from a strategy program state.
pub type StrategyStopFn =
    unsafe extern "C" fn(state: *mut c_void, metrics: *const CMetricSink) -> i32;

/// Destroy a strategy program state.
pub type StrategyDestroyFn = unsafe extern "C" fn(state: *mut c_void);

/// The vtable a screening artifact exports.
///
/// A screening program has no order surface by construction: the vtable
/// carries exactly the four one-shot callbacks (`create`, `screen`,
/// `result`, `destroy`) and nothing else. The host serializes one context
/// JSON document, calls `screen`, then copies the `result` payload before
/// the next call or destroy.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct ScreeningProgramVTable {
    /// ABI version this vtable was built for.
    pub abi_version: u32,
    /// Reserved for tail growth; must stay zero.
    pub reserved: u32,
    /// Construct the program state from a JSON parameters document.
    pub create: ScreeningCreateFn,
    /// Evaluate one context JSON document.
    pub screen: ScreeningScreenFn,
    /// Borrow the last serialized result payload.
    pub result: ScreeningResultFn,
    /// Destroy the program state.
    pub destroy: ScreeningDestroyFn,
}

/// Construct a screening program state from a JSON parameters document.
///
/// Returns the state pointer, or null on failure (details via
/// `harness_sdk_last_error`).
pub type ScreeningCreateFn = unsafe extern "C" fn(parameters: CStr) -> *mut c_void;

/// Evaluate one screening context JSON document into the program state.
///
/// Returns [`SDK_OK`], or [`SDK_ERROR`] when the context does not parse, the
/// authored program fails or panics, or the result cannot be serialized
/// (details via `harness_sdk_last_error`).
pub type ScreeningScreenFn = unsafe extern "C" fn(state: *mut c_void, ctx_json: CStr) -> i32;

/// Borrow the last serialized result payload from the program state.
///
/// The returned view stays valid until the next `screen` call or `destroy` on
/// the same state; the host copies the pointer and length immediately.
pub type ScreeningResultFn = unsafe extern "C" fn(state: *mut c_void) -> CStr;

/// Destroy a screening program state.
pub type ScreeningDestroyFn = unsafe extern "C" fn(state: *mut c_void);

/// The exported ABI-version symbol.
pub const ABI_EXPORT: &str = "harness_sdk_abi_version";

/// The exported study vtable symbol.
pub const STUDY_VTABLE_EXPORT: &str = "harness_sdk_study_vtable";

/// The exported strategy vtable symbol.
pub const STRATEGY_VTABLE_EXPORT: &str = "harness_sdk_strategy_vtable";

/// The exported screening vtable symbol.
pub const SCREENING_VTABLE_EXPORT: &str = "harness_sdk_screening_vtable";

/// The exported last-error symbol.
pub const LAST_ERROR_EXPORT: &str = "harness_sdk_last_error";
