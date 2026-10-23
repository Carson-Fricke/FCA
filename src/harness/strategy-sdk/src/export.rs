//! The entry-point macros and their panic-guarded trampolines.
//!
//! [`export_study_program!`], [`export_strategy_program!`], and
//! [`export_screening_program!`] turn one authored type into the exported
//! dylib symbols. Every author callback runs inside
//! [`std::panic::catch_unwind`]; a panic or an explicit failure is
//! recorded in a thread-local slot readable through the exported
//! `harness_sdk_last_error` symbol and mapped to [`SDK_ERROR`] by the host.
//!
//! One authored crate exports exactly one program kind: the three macros all
//! define the handshake symbols, so combining them in one crate is a duplicate
//! symbol error by construction.

// The trampolines below are reachable only through the exported macros, which
// expand in authored crates; compiling this library standalone sees them as
// dead code.
#![allow(dead_code)]

use std::any::Any;
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::ffi::c_char;
use std::ffi::CString;
use std::panic::AssertUnwindSafe;

use crate::abi::CBarEvent;
use crate::abi::CBarFrame;
use crate::abi::CFill;
use crate::abi::CMetricSink;
use crate::abi::COrderSink;
use crate::abi::CQuoteEvent;
use crate::abi::CStart;
use crate::abi::CStr;
use crate::abi::CTradeEvent;
use crate::abi::SDK_ERROR;
use crate::abi::SDK_OK;
use crate::consumer::route_bar_with_signature;
use crate::consumer::route_quote_with_signature;
use crate::consumer::route_trade_with_signature;
use crate::consumer::routing_signature;
use crate::consumer::validate_routing;
use crate::consumer::RoutingSignature;
use crate::effective_subscriptions;
use crate::events::BarEvent;
use crate::events::BarFrame;
use crate::events::Fill;
use crate::events::ProgramStart;
use crate::events::QuoteEvent;
use crate::events::TradeEvent;
use crate::program::MetricPublisher;
use crate::program::OrderSink;
use crate::program::ScreenContext;
use crate::program::ScreeningProgram;
use crate::program::StrategyProgram;
use crate::program::StudyProgram;
use crate::MarketConsumer;
use crate::ObservationContext;

struct ConsumerState<T> {
    program: T,
    subscriptions: String,
    signature: Option<RoutingSignature>,
    failed: bool,
}

unsafe fn borrow_program<'a, T: MarketConsumer>(
    state: *mut std::os::raw::c_void,
    what: &str,
) -> Option<(&'a mut T, &'a RoutingSignature)> {
    let state = borrow_state::<ConsumerState<T>>(state, what)?;
    if state.failed {
        record_error("market consumer previously failed routing validation".into());
        return None;
    }
    let result = std::panic::catch_unwind(AssertUnwindSafe(|| {
        validate_routing(
            &mut state.program,
            state.signature.as_ref().expect("constructed signature"),
        )
    }));
    let result = match result {
        Ok(result) => result,
        Err(payload) => {
            record_panic(&*payload);
            state.failed = true;
            return None;
        }
    };
    if let Err(message) = result {
        state.failed = true;
        record_error(message);
        return None;
    }
    Some((&mut state.program, state.signature.as_ref()?))
}

fn create_consumer<T: MarketConsumer>(program: T) -> *mut std::os::raw::c_void {
    // Capture addresses only after the final allocation, not before moving the root.
    let mut state = Box::new(ConsumerState {
        program,
        subscriptions: String::new(),
        signature: None,
        failed: false,
    });
    match routing_signature(&mut state.program)
        .and_then(|signature| {
            state.signature = Some(signature);
            effective_subscriptions(&mut state.program)
        })
        .and_then(|inputs| serde_json::to_string(&inputs).map_err(|e| e.to_string()))
        .and_then(|subscriptions| {
            validate_routing(
                &mut state.program,
                state.signature.as_ref().expect("captured signature"),
            )?;
            Ok(subscriptions)
        }) {
        Ok(subscriptions) => {
            state.subscriptions = subscriptions;
            Box::into_raw(state).cast()
        }
        Err(message) => {
            record_error(format!("invalid subscriptions: {message}"));
            std::ptr::null_mut()
        }
    }
}

unsafe fn finish_callback<T: MarketConsumer>(
    state: *mut std::os::raw::c_void,
    result: Result<(), String>,
) -> i32 {
    let Some(state) = borrow_state::<ConsumerState<T>>(state, "consumer") else {
        return SDK_ERROR;
    };
    let result = result.and_then(|()| {
        validate_routing(
            &mut state.program,
            state.signature.as_ref().expect("constructed signature"),
        )
    });
    match result {
        Ok(()) => SDK_OK,
        Err(message) => {
            state.failed = true;
            record_error(message);
            SDK_ERROR
        }
    }
}

/// Borrow the state-owned effective subscription JSON.
///
/// # Safety
/// State must come from the matching consumer create trampoline.
pub unsafe extern "C" fn __subscriptions<T: MarketConsumer>(
    state: *mut std::os::raw::c_void,
) -> CStr {
    match borrow_state::<ConsumerState<T>>(state, "consumer") {
        Some(state) => CStr::from_slice(&state.subscriptions),
        None => CStr::from_slice(""),
    }
}

/// Observe a study quote with dependency routing.
///
/// # Safety
/// State and event must be valid for the callback.
pub unsafe extern "C" fn __study_on_quote<T: StudyProgram>(
    state: *mut std::os::raw::c_void,
    event: *const CQuoteEvent,
) -> i32 {
    run_guarded(|| {
        let (Some((program, signature)), Some(event)) =
            (borrow_program::<T>(state, "study"), quote_view(event))
        else {
            record_error("study quote event or state is malformed".into());
            return SDK_ERROR;
        };
        let result =
            route_quote_with_signature(program, &event, &mut ObservationContext, signature);
        finish_callback::<T>(state, result)
    })
}

/// Observe a study trade with dependency routing.
///
/// # Safety
/// State and event must be valid for the callback.
pub unsafe extern "C" fn __study_on_trade<T: StudyProgram>(
    state: *mut std::os::raw::c_void,
    event: *const CTradeEvent,
) -> i32 {
    run_guarded(|| {
        let (Some((program, signature)), Some(event)) =
            (borrow_program::<T>(state, "study"), trade_view(event))
        else {
            record_error("study trade event or state is malformed".into());
            return SDK_ERROR;
        };
        let result =
            route_trade_with_signature(program, &event, &mut ObservationContext, signature);
        finish_callback::<T>(state, result)
    })
}

/// Observe a strategy frame and preserve submitted orders.
///
/// # Safety
/// State, frame storage, and order sink must be valid for the callback.
pub unsafe extern "C" fn __strategy_on_bar_frame<T: StrategyProgram>(
    state: *mut std::os::raw::c_void,
    frame: *const CBarFrame,
    orders: *const COrderSink,
) -> i32 {
    run_guarded(|| {
        let (Some((program, signature)), Some(frame), Some(sink)) = (
            borrow_program::<T>(state, "strategy"),
            bar_frame_view(frame),
            orders.as_ref(),
        ) else {
            record_error("strategy frame, state or order sink is malformed".into());
            return SDK_ERROR;
        };
        let result =
            route_bar_with_signature(program, &frame, &mut OrderSink::from_sink(*sink), signature);
        finish_callback::<T>(state, result)
    })
}

thread_local! {
    static LAST_ERROR: RefCell<Option<CString>> = const { RefCell::new(None) };
}

/// Record the last SDK error message on the current thread.
pub fn record_error(message: String) {
    let stored = CString::new(message)
        .unwrap_or_else(|_| CString::new("SDK error").expect("literal has no NUL"));
    LAST_ERROR.with(|slot| *slot.borrow_mut() = Some(stored));
}

/// A pointer to the NUL-terminated last error for the current thread, or null
/// when nothing failed. The pointer stays valid until the next SDK call on the
/// same thread; the host copies it immediately after a failing call.
#[must_use]
pub fn last_error_ptr() -> *const c_char {
    LAST_ERROR.with(|slot| {
        slot.borrow()
            .as_ref()
            .map_or(std::ptr::null(), |message| message.as_ptr())
    })
}

fn record_panic(payload: &(dyn Any + Send)) {
    let message = payload
        .downcast_ref::<&str>()
        .map(|text| (*text).to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "authored program panicked".to_string());
    record_error(format!("authored program failed: {message}"));
}

fn run_guarded(run: impl FnOnce() -> i32) -> i32 {
    match std::panic::catch_unwind(AssertUnwindSafe(run)) {
        Ok(code) => code,
        Err(payload) => {
            record_panic(&*payload);
            SDK_ERROR
        }
    }
}

unsafe fn borrow_state<'a, T>(state: *mut std::os::raw::c_void, what: &str) -> Option<&'a mut T> {
    if state.is_null() {
        record_error(format!("{what} state is null"));
        return None;
    }
    Some(&mut *(state.cast::<T>()))
}

/// # Safety
///
/// `event` must point to a valid `CQuoteEvent` whose string views remain
/// readable for the callback duration.
pub unsafe fn quote_view<'a>(event: *const CQuoteEvent) -> Option<QuoteEvent<'a>> {
    let raw = unsafe { event.as_ref()? };
    Some(QuoteEvent {
        event_id: unsafe { raw.event_id.as_str() }?,
        timestamp_ns: raw.timestamp_ns,
        sequence: raw.sequence,
        symbol: unsafe { raw.symbol.as_str() }?,
        bid: raw.bid.to_decimal(),
        ask: raw.ask.to_decimal(),
        bid_quantity: raw.bid_quantity.to_decimal(),
        ask_quantity: raw.ask_quantity.to_decimal(),
        venue: unsafe { raw.venue.as_str() }?,
    })
}

/// # Safety
///
/// `event` must point to a valid `CTradeEvent` whose string views remain
/// readable for the callback duration.
pub unsafe fn trade_view<'a>(event: *const CTradeEvent) -> Option<TradeEvent<'a>> {
    let raw = unsafe { event.as_ref()? };
    Some(TradeEvent {
        event_id: unsafe { raw.event_id.as_str() }?,
        timestamp_ns: raw.timestamp_ns,
        sequence: raw.sequence,
        symbol: unsafe { raw.symbol.as_str() }?,
        price: raw.price.to_decimal(),
        quantity: raw.quantity.to_decimal(),
        venue: unsafe { raw.venue.as_str() }?,
        trade_id: unsafe { raw.trade_id.as_str() }?,
    })
}

/// # Safety
///
/// `event` must point to a valid `CBarEvent` whose string views remain
/// readable for the callback duration.
pub unsafe fn bar_view<'a>(event: *const CBarEvent) -> Option<BarEvent<'a>> {
    let raw = unsafe { event.as_ref()? };
    Some(BarEvent {
        event_id: unsafe { raw.event_id.as_str() }?,
        timestamp_ns: raw.timestamp_ns,
        sequence: raw.sequence,
        symbol: unsafe { raw.symbol.as_str() }?,
        open: raw.open.to_decimal(),
        high: raw.high.to_decimal(),
        low: raw.low.to_decimal(),
        close: raw.close.to_decimal(),
        volume: raw.volume.to_decimal(),
    })
}

unsafe fn checked_slice<'a, T>(ptr: *const T, len: usize) -> Option<&'a [T]> {
    if len == 0 {
        return Some(&[]);
    }
    let bytes = len.checked_mul(std::mem::size_of::<T>())?;
    if ptr.is_null()
        || !(ptr as usize).is_multiple_of(std::mem::align_of::<T>())
        || bytes > isize::MAX as usize
        || (ptr as usize).checked_add(bytes).is_none()
    {
        return None;
    }
    Some(unsafe { std::slice::from_raw_parts(ptr, len) })
}

/// Validate and reconstruct a frame without exposing Rust containers over FFI.
///
/// # Safety
///
/// Non-null, aligned pointers with representable lengths must reference live,
/// initialized storage readable for the returned view's lifetime. Structural
/// validation cannot establish whether a foreign allocation actually exists.
pub unsafe fn bar_frame_view<'a>(frame: *const CBarFrame) -> Option<BarFrame<'a>> {
    let raw = unsafe { checked_slice(frame, 1) }?.first()?;
    if raw.interval_end_ns <= raw.interval_start_ns || raw.available_at_ns < raw.interval_end_ns {
        return None;
    }
    let bars = unsafe { checked_slice(raw.bars, raw.bars_len) }?;
    let missing = unsafe { checked_slice(raw.missing_symbols, raw.missing_symbols_len) }?;
    let mut bars_by_symbol = BTreeMap::new();
    for raw_bar in bars {
        let bar = unsafe { bar_view(raw_bar) }?;
        if bar.timestamp_ns != raw.interval_start_ns
            || bars_by_symbol.insert(bar.symbol, bar).is_some()
        {
            return None;
        }
    }
    let mut missing_symbols = Vec::new();
    let mut seen = BTreeSet::new();
    for raw_symbol in missing {
        let symbol = unsafe { raw_symbol.as_str() }?;
        if bars_by_symbol.contains_key(symbol) || !seen.insert(symbol) {
            return None;
        }
        missing_symbols.push(symbol);
    }
    Some(BarFrame {
        interval: match unsafe { raw.interval.as_str() }? {
            interval if harness_contracts::completed_bar_interval_ns(interval).is_ok() => interval,
            _ => return None,
        },
        interval_start_ns: raw.interval_start_ns,
        interval_end_ns: raw.interval_end_ns,
        available_at_ns: raw.available_at_ns,
        bars_by_symbol,
        missing_symbols,
    })
}

/// # Safety
///
/// `fill` must point to a valid `CFill` whose string views remain readable
/// for the callback duration.
pub unsafe fn fill_view<'a>(fill: *const CFill) -> Option<Fill<'a>> {
    let raw = unsafe { fill.as_ref()? };
    Some(Fill {
        event_id: unsafe { raw.event_id.as_str() }?,
        timestamp_ns: raw.timestamp_ns,
        sequence: raw.sequence,
        fill_id: unsafe { raw.fill_id.as_str() }?,
        order_id: unsafe { raw.order_id.as_str() }?,
        symbol: unsafe { raw.symbol.as_str() }?,
        side: unsafe { raw.side.as_str() }?,
        quantity: raw.quantity.to_decimal(),
        price: raw.price.to_decimal(),
        fee: raw.fee.to_decimal(),
        liquidity: unsafe { raw.liquidity.as_str() }?,
    })
}

unsafe fn start_symbols(start: &CStart) -> Option<Vec<&str>> {
    let slice = unsafe { std::slice::from_raw_parts(start.symbols.ptr, start.symbols.len) };
    slice
        .iter()
        .map(|symbol| unsafe { symbol.as_str() })
        .collect()
}

fn parse_parameters(parameters: CStr) -> Result<serde_json::Value, String> {
    let text = unsafe { parameters.as_str() }
        .ok_or_else(|| "parameters are not valid UTF-8".to_string())?;
    serde_json::from_str(text).map_err(|error| format!("parameters are not valid JSON: {error}"))
}

/// Trampoline: construct a study program from JSON parameters.
///
/// # Safety
///
/// `parameters` must be a valid [`CStr`].
pub unsafe extern "C" fn __study_create<T: StudyProgram>(
    parameters: CStr,
) -> *mut std::os::raw::c_void {
    let outcome =
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            match parse_parameters(parameters) {
                Ok(value) => create_consumer(T::new(value)),
                Err(message) => {
                    record_error(format!("study program creation failed: {message}"));
                    std::ptr::null_mut()
                }
            }
        }));
    match outcome {
        Ok(pointer) => pointer,
        Err(payload) => {
            record_panic(&*payload);
            std::ptr::null_mut()
        }
    }
}

/// Trampoline: fold one atomic interval frame into a study program.
///
/// # Safety
///
/// `state` must be the pointer [`__study_create`] returned and `frame` must
/// meet the storage requirements of [`bar_frame_view`].
pub unsafe extern "C" fn __study_on_bar_frame<T: StudyProgram>(
    state: *mut std::os::raw::c_void,
    frame: *const CBarFrame,
) -> i32 {
    run_guarded(|| {
        let Some((program, signature)) = (unsafe { borrow_program::<T>(state, "study program") })
        else {
            return SDK_ERROR;
        };
        let Some(view) = (unsafe { bar_frame_view(frame) }) else {
            record_error("study bar frame is malformed".to_string());
            return SDK_ERROR;
        };
        let result = route_bar_with_signature(program, &view, &mut ObservationContext, signature);
        unsafe { finish_callback::<T>(state, result) }
    })
}

/// Trampoline: publish final metrics from a study program.
///
/// # Safety
///
/// `state` must be the pointer [`__study_create`] returned and `metrics`
/// must point to a valid host sink.
pub unsafe extern "C" fn __study_on_stop<T: StudyProgram>(
    state: *mut std::os::raw::c_void,
    metrics: *const CMetricSink,
) -> i32 {
    let Some((program, _)) = (unsafe { borrow_program::<T>(state, "study program") }) else {
        return SDK_ERROR;
    };
    let Some(sink) = (unsafe { metrics.as_ref() }) else {
        record_error("study metric sink is null".to_string());
        return SDK_ERROR;
    };
    let mut publisher = MetricPublisher::from_sink(*sink);
    run_guarded(|| {
        if let Err(error) = crate::outputs::publish_indicator_outputs(program, &mut publisher) {
            return unsafe { finish_callback::<T>(state, Err(error)) };
        }
        program.on_stop(&mut publisher);
        unsafe { finish_callback::<T>(state, Ok(())) }
    })
}

/// Trampoline: destroy a study program state.
///
/// # Safety
///
/// `state` must be the pointer [`__study_create`] returned, and it must not
/// be used again afterwards.
pub unsafe extern "C" fn __study_destroy<T: StudyProgram>(state: *mut std::os::raw::c_void) {
    if state.is_null() {
        return;
    }
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        drop(Box::from_raw(state.cast::<ConsumerState<T>>()));
    }));
}

/// Trampoline: construct a strategy program from JSON parameters.
///
/// # Safety
///
/// `parameters` must be a valid [`CStr`].
pub unsafe extern "C" fn __strategy_create<T: StrategyProgram>(
    parameters: CStr,
) -> *mut std::os::raw::c_void {
    let outcome =
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            match parse_parameters(parameters) {
                Ok(value) => create_consumer(T::new(value)),
                Err(message) => {
                    record_error(format!("strategy program creation failed: {message}"));
                    std::ptr::null_mut()
                }
            }
        }));
    match outcome {
        Ok(pointer) => pointer,
        Err(payload) => {
            record_panic(&*payload);
            std::ptr::null_mut()
        }
    }
}

/// Trampoline: run one strategy program start.
///
/// # Safety
///
/// `state` must be the pointer [`__strategy_create`] returned and `start`
/// must point to a valid `CStart`.
pub unsafe extern "C" fn __strategy_on_start<T: StrategyProgram>(
    state: *mut std::os::raw::c_void,
    start: *const CStart,
) -> i32 {
    let Some((program, _)) = (unsafe { borrow_program::<T>(state, "strategy program") }) else {
        return SDK_ERROR;
    };
    let Some(raw) = (unsafe { start.as_ref() }) else {
        record_error("strategy start view is malformed".to_string());
        return SDK_ERROR;
    };
    let Some(symbols) = (unsafe { start_symbols(raw) }) else {
        record_error("strategy start view is malformed".to_string());
        return SDK_ERROR;
    };
    let Some(instance_id) = (unsafe { raw.instance_id.as_str() }) else {
        record_error("strategy start view is malformed".to_string());
        return SDK_ERROR;
    };
    let Some(account_id) = (unsafe { raw.account_id.as_str() }) else {
        record_error("strategy start view is malformed".to_string());
        return SDK_ERROR;
    };
    let start_view = ProgramStart {
        instance_id,
        account_id,
        symbols: &symbols,
        seed: raw.seed,
        initial_cash: raw.initial_cash.to_decimal(),
        start_ns: raw.start_ns,
    };
    run_guarded(|| {
        program.on_start(&start_view);
        unsafe { finish_callback::<T>(state, Ok(())) }
    })
}

/// Trampoline: fold one quote into a strategy program and submit its orders.
///
/// # Safety
///
/// `state` must be the pointer [`__strategy_create`] returned, `event` must
/// point to a valid `CQuoteEvent`, and `orders` must point to a valid host
/// sink.
pub unsafe extern "C" fn __strategy_on_quote<T: StrategyProgram>(
    state: *mut std::os::raw::c_void,
    event: *const CQuoteEvent,
    orders: *const COrderSink,
) -> i32 {
    let Some((program, signature)) = (unsafe { borrow_program::<T>(state, "strategy program") })
    else {
        return SDK_ERROR;
    };
    let Some(quote) = (unsafe { quote_view(event) }) else {
        record_error("strategy quote event is malformed".to_string());
        return SDK_ERROR;
    };
    let Some(sink) = (unsafe { orders.as_ref() }) else {
        record_error("strategy order sink is null".to_string());
        return SDK_ERROR;
    };
    let mut orders = OrderSink::from_sink(*sink);
    run_guarded(|| {
        let result = route_quote_with_signature(program, &quote, &mut orders, signature);
        unsafe { finish_callback::<T>(state, result) }
    })
}

/// Trampoline: fold one trade into a strategy program and submit its orders.
///
/// # Safety
///
/// `state` must be the pointer [`__strategy_create`] returned, `event` must
/// point to a valid `CTradeEvent`, and `orders` must point to a valid host
/// sink.
pub unsafe extern "C" fn __strategy_on_trade<T: StrategyProgram>(
    state: *mut std::os::raw::c_void,
    event: *const CTradeEvent,
    orders: *const COrderSink,
) -> i32 {
    let Some((program, signature)) = (unsafe { borrow_program::<T>(state, "strategy program") })
    else {
        return SDK_ERROR;
    };
    let Some(trade) = (unsafe { trade_view(event) }) else {
        record_error("strategy trade event is malformed".to_string());
        return SDK_ERROR;
    };
    let Some(sink) = (unsafe { orders.as_ref() }) else {
        record_error("strategy order sink is null".to_string());
        return SDK_ERROR;
    };
    let mut orders = OrderSink::from_sink(*sink);
    run_guarded(|| {
        let result = route_trade_with_signature(program, &trade, &mut orders, signature);
        unsafe { finish_callback::<T>(state, result) }
    })
}

/// Trampoline: fold one booked fill into a strategy program.
///
/// # Safety
///
/// `state` must be the pointer [`__strategy_create`] returned and `fill`
/// must point to a valid `CFill`.
pub unsafe extern "C" fn __strategy_on_fill<T: StrategyProgram>(
    state: *mut std::os::raw::c_void,
    fill: *const CFill,
) -> i32 {
    let Some((program, _)) = (unsafe { borrow_program::<T>(state, "strategy program") }) else {
        return SDK_ERROR;
    };
    let Some(view) = (unsafe { fill_view(fill) }) else {
        record_error("strategy fill view is malformed".to_string());
        return SDK_ERROR;
    };
    run_guarded(|| {
        program.on_fill(&view);
        unsafe { finish_callback::<T>(state, Ok(())) }
    })
}

/// Trampoline: publish final metrics from a strategy program.
///
/// # Safety
///
/// `state` must be the pointer [`__strategy_create`] returned and `metrics`
/// must point to a valid host sink.
pub unsafe extern "C" fn __strategy_on_stop<T: StrategyProgram>(
    state: *mut std::os::raw::c_void,
    metrics: *const CMetricSink,
) -> i32 {
    let Some((program, _)) = (unsafe { borrow_program::<T>(state, "strategy program") }) else {
        return SDK_ERROR;
    };
    let Some(sink) = (unsafe { metrics.as_ref() }) else {
        record_error("strategy metric sink is null".to_string());
        return SDK_ERROR;
    };
    let mut publisher = MetricPublisher::from_sink(*sink);
    run_guarded(|| {
        if let Err(error) = crate::outputs::publish_indicator_outputs(program, &mut publisher) {
            return unsafe { finish_callback::<T>(state, Err(error)) };
        }
        program.on_stop(&mut publisher);
        unsafe { finish_callback::<T>(state, Ok(())) }
    })
}

/// Trampoline: destroy a strategy program state.
///
/// # Safety
///
/// `state` must be the pointer [`__strategy_create`] returned, and it must
/// not be used again afterwards.
pub unsafe extern "C" fn __strategy_destroy<T: StrategyProgram>(state: *mut std::os::raw::c_void) {
    if state.is_null() {
        return;
    }
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        drop(Box::from_raw(state.cast::<ConsumerState<T>>()));
    }));
}

/// The state a screening trampoline owns: the authored program plus the last
/// serialized result payload.
///
/// The result buffer is what [`__screening_result`] borrows; it stays valid
/// until the next `screen` call or `destroy`.
struct ScreeningState<T: ScreeningProgram> {
    program: T,
    result: Option<CString>,
}

/// Trampoline: construct a screening program from JSON parameters.
///
/// # Safety
///
/// `parameters` must be a valid [`CStr`].
pub unsafe extern "C" fn __screening_create<T: ScreeningProgram>(
    parameters: CStr,
) -> *mut std::os::raw::c_void {
    let outcome =
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            match parse_parameters(parameters) {
                Ok(value) => Box::into_raw(Box::new(ScreeningState {
                    program: T::new(value),
                    result: None,
                }))
                .cast::<std::os::raw::c_void>(),
                Err(message) => {
                    record_error(format!("screening program creation failed: {message}"));
                    std::ptr::null_mut()
                }
            }
        }));
    match outcome {
        Ok(pointer) => pointer,
        Err(payload) => {
            record_panic(&*payload);
            std::ptr::null_mut()
        }
    }
}

/// Trampoline: evaluate one screening context into the program state.
///
/// A context that does not parse or a result that cannot be serialized is
/// recorded through [`record_error`] and mapped to [`SDK_ERROR`]; the
/// authored program itself runs inside `run_guarded`, so it never panics
/// across the boundary.
///
/// # Safety
///
/// `state` must be the pointer [`__screening_create`] returned and
/// `ctx_json` must be a valid [`CStr`].
pub unsafe extern "C" fn __screening_screen<T: ScreeningProgram>(
    state: *mut std::os::raw::c_void,
    ctx_json: CStr,
) -> i32 {
    let Some(state) = (unsafe { borrow_state::<ScreeningState<T>>(state, "screening program") })
    else {
        return SDK_ERROR;
    };
    let Some(text) = (unsafe { ctx_json.as_str() }) else {
        record_error("screening context is not valid UTF-8".to_string());
        return SDK_ERROR;
    };
    let context = match serde_json::from_str::<ScreenContext>(text) {
        Ok(context) => context,
        Err(error) => {
            record_error(format!("screening context is not valid JSON: {error}"));
            return SDK_ERROR;
        }
    };
    run_guarded(|| {
        let rows = state.program.screen(&context);
        match serde_json::to_string(&rows) {
            Ok(document) => match CString::new(document) {
                Ok(result) => {
                    state.result = Some(result);
                    SDK_OK
                }
                Err(error) => {
                    record_error(format!("screening result could not be encoded: {error}"));
                    SDK_ERROR
                }
            },
            Err(error) => {
                record_error(format!("screening result could not be serialized: {error}"));
                SDK_ERROR
            }
        }
    })
}

/// Trampoline: borrow the last serialized screening result payload.
///
/// The returned view stays valid until the next `screen` call or `destroy`
/// on the same state; the host copies the pointer and length immediately.
///
/// # Safety
///
/// `state` must be the pointer [`__screening_create`] returned.
pub unsafe extern "C" fn __screening_result<T: ScreeningProgram>(
    state: *mut std::os::raw::c_void,
) -> CStr {
    let Some(state) = (unsafe { borrow_state::<ScreeningState<T>>(state, "screening program") })
    else {
        return CStr::from_slice("");
    };
    match &state.result {
        Some(result) => CStr {
            ptr: result.as_ptr().cast::<u8>(),
            len: result.as_bytes().len(),
        },
        None => {
            record_error("screening result requested before screen completed".to_string());
            CStr::from_slice("")
        }
    }
}

/// Trampoline: destroy a screening program state.
///
/// # Safety
///
/// `state` must be the pointer [`__screening_create`] returned, and it must
/// not be used again afterwards.
pub unsafe extern "C" fn __screening_destroy<T: ScreeningProgram>(
    state: *mut std::os::raw::c_void,
) {
    if state.is_null() {
        return;
    }
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        drop(Box::from_raw(state.cast::<ScreeningState<T>>()));
    }));
}

/// Export one authored [`StudyProgram`] as the artifact's program surface.
///
/// Generates the handshake exports (`harness_sdk_abi_version`,
/// `harness_sdk_last_error`, `harness_sdk_study_vtable`) and the vtable
/// trampolines for the type.
#[macro_export]
macro_rules! export_study_program {
    ($ty:ty) => {
        #[no_mangle]
        pub extern "C" fn harness_sdk_abi_version() -> u32 {
            $crate::SDK_ABI_VERSION
        }

        #[no_mangle]
        pub extern "C" fn harness_sdk_last_error() -> *const ::std::os::raw::c_char {
            $crate::export::last_error_ptr()
        }

        #[no_mangle]
        pub extern "C" fn harness_sdk_study_vtable() -> *const $crate::StudyProgramVTable {
            static VTABLE: $crate::StudyProgramVTable = $crate::StudyProgramVTable {
                abi_version: $crate::SDK_ABI_VERSION,
                reserved: 0,
                create: $crate::export::__study_create::<$ty>,
                on_bar_frame: $crate::export::__study_on_bar_frame::<$ty>,
                on_stop: $crate::export::__study_on_stop::<$ty>,
                destroy: $crate::export::__study_destroy::<$ty>,
                on_quote: $crate::export::__study_on_quote::<$ty>,
                on_trade: $crate::export::__study_on_trade::<$ty>,
                subscriptions: $crate::export::__subscriptions::<$ty>,
            };
            &VTABLE
        }
    };
}

/// Export one authored [`StrategyProgram`] as the artifact's program surface.
///
/// See [`export_study_program!`] for the one-program-per-crate contract.
#[macro_export]
macro_rules! export_strategy_program {
    ($ty:ty) => {
        #[no_mangle]
        pub extern "C" fn harness_sdk_abi_version() -> u32 {
            $crate::SDK_ABI_VERSION
        }

        #[no_mangle]
        pub extern "C" fn harness_sdk_last_error() -> *const ::std::os::raw::c_char {
            $crate::export::last_error_ptr()
        }

        #[no_mangle]
        pub extern "C" fn harness_sdk_strategy_vtable() -> *const $crate::StrategyProgramVTable {
            static VTABLE: $crate::StrategyProgramVTable = $crate::StrategyProgramVTable {
                abi_version: $crate::SDK_ABI_VERSION,
                reserved: 0,
                create: $crate::export::__strategy_create::<$ty>,
                on_start: $crate::export::__strategy_on_start::<$ty>,
                on_quote: $crate::export::__strategy_on_quote::<$ty>,
                on_trade: $crate::export::__strategy_on_trade::<$ty>,
                on_fill: $crate::export::__strategy_on_fill::<$ty>,
                on_stop: $crate::export::__strategy_on_stop::<$ty>,
                destroy: $crate::export::__strategy_destroy::<$ty>,
                on_bar_frame: $crate::export::__strategy_on_bar_frame::<$ty>,
                subscriptions: $crate::export::__subscriptions::<$ty>,
            };
            &VTABLE
        }
    };
}

/// Export one authored [`ScreeningProgram`] as the artifact's program surface.
///
/// Generates the handshake exports (`harness_sdk_abi_version`,
/// `harness_sdk_last_error`, `harness_sdk_screening_vtable`) and the vtable
/// trampolines for the type. The screening vtable carries no order surface by
/// construction.
///
/// See [`export_study_program!`] for the one-program-per-crate contract.
#[macro_export]
macro_rules! export_screening_program {
    ($ty:ty) => {
        #[no_mangle]
        pub extern "C" fn harness_sdk_abi_version() -> u32 {
            $crate::SDK_ABI_VERSION
        }

        #[no_mangle]
        pub extern "C" fn harness_sdk_last_error() -> *const ::std::os::raw::c_char {
            $crate::export::last_error_ptr()
        }

        #[no_mangle]
        pub extern "C" fn harness_sdk_screening_vtable() -> *const $crate::ScreeningProgramVTable {
            static VTABLE: $crate::ScreeningProgramVTable = $crate::ScreeningProgramVTable {
                abi_version: $crate::SDK_ABI_VERSION,
                reserved: 0,
                create: $crate::export::__screening_create::<$ty>,
                screen: $crate::export::__screening_screen::<$ty>,
                result: $crate::export::__screening_result::<$ty>,
                destroy: $crate::export::__screening_destroy::<$ty>,
            };
            &VTABLE
        }
    };
}
