//! Engine runtime adapters over loaded authored programs.
//!
//! These implement the engine's `StrategyRuntime` seam with the exact
//! capability split the Python adapters established: a study runtime folds
//! quotes, trades, and atomic bar frames and never returns orders; a strategy submits
//! intents the host stamps with the event time, and every metric publish is
//! validated host-side so a rejected metric fails the run.

use std::collections::BTreeMap;
use std::os::raw::c_void;

use backtest::backtest::engine_core::{
    EngineBar, EngineBarFrame, EngineFill, EngineMarket, EngineOrder, MarketSubscription,
    StrategyInstance, StrategyRuntime, StrategyStart,
};
use backtest::backtest::errors::BacktestError;
use harness_strategy_sdk::{
    canonical_subscriptions, CBarEvent, CBarFrame, CDecimal, CFill, CMarketOrder, CMetricSink,
    COrderSink, CQuoteEvent, CStart, CStr, CTradeEvent, MetricPublishFn, OrderSubmitFn,
    Subscription, METRIC_INVALID_NAME, METRIC_NOT_FINITE, METRIC_OK, METRIC_RESERVED_NAME,
    RESERVED_METRIC_NAMES, SDK_OK,
};

use crate::compile::{compile, ProgramKind};
use crate::error::JitError;
use crate::load::{LoadedStrategy, LoadedStudy};
use crate::workspace::ManagedWorkspace;

/// The metric sink result code for any other rejection.
const METRIC_REJECTED: i32 = 4;
/// The order sink result code for any rejection.
const ORDER_REJECTED: i32 = 1;

/// The failure for raw individual bars rather than atomic frames.
///
/// Quotes and trades use the shared consumer callbacks; bars must be atomic.
pub const STUDY_BARS_ONLY: &str = "raw bars must be delivered as atomic bar frames";

fn read_subscriptions(view: CStr) -> Result<Vec<MarketSubscription>, JitError> {
    let text = unsafe { view.as_str() }
        .ok_or_else(|| JitError::Invalid("subscription JSON is not valid UTF-8".into()))?;
    let inputs: Vec<Subscription> = serde_json::from_str(text)
        .map_err(|error| JitError::Invalid(format!("invalid subscriptions: {error}")))?;
    let canonical = canonical_subscriptions(inputs).map_err(JitError::Invalid)?;
    Ok(canonical.into_iter().map(engine_subscription).collect())
}

pub(crate) fn engine_subscription(input: Subscription) -> MarketSubscription {
    match input {
        Subscription::Bars { symbols, interval } => MarketSubscription {
            data_type: "bars".into(),
            symbols,
            interval: Some(interval),
        },
        Subscription::Quote { symbols } => MarketSubscription {
            data_type: "quote".into(),
            symbols,
            interval: None,
        },
        Subscription::Trade { symbols } => MarketSubscription {
            data_type: "trade".into(),
            symbols,
            interval: None,
        },
    }
}

fn frame_storage(frame: &EngineBarFrame) -> Result<(Vec<CBarEvent>, Vec<CStr>), BacktestError> {
    if frame
        .bars_by_symbol
        .iter()
        .any(|(symbol, bar)| symbol != &bar.symbol)
    {
        return Err(BacktestError::StrategyExecution(
            "bar frame map key does not match bar symbol".into(),
        ));
    }
    let bars = frame
        .bars_by_symbol
        .values()
        .map(|bar| CBarEvent {
            event_id: CStr::from_slice(&bar.event_id),
            timestamp_ns: bar.timestamp_ns,
            sequence: bar.sequence,
            symbol: CStr::from_slice(&bar.symbol),
            open: CDecimal::from_decimal(bar.open),
            high: CDecimal::from_decimal(bar.high),
            low: CDecimal::from_decimal(bar.low),
            close: CDecimal::from_decimal(bar.close),
            volume: CDecimal::from_decimal(bar.volume),
        })
        .collect();
    let missing = frame
        .missing_symbols
        .iter()
        .map(|symbol| CStr::from_slice(symbol))
        .collect();
    Ok((bars, missing))
}

fn frame_view(frame: &EngineBarFrame, bars: &[CBarEvent], missing: &[CStr]) -> CBarFrame {
    CBarFrame {
        interval: CStr::from_slice(&frame.interval),
        interval_start_ns: frame.interval_start_ns,
        interval_end_ns: frame.interval_end_ns,
        available_at_ns: frame.available_at_ns,
        bars: bars.as_ptr(),
        bars_len: bars.len(),
        missing_symbols: missing.as_ptr(),
        missing_symbols_len: missing.len(),
    }
}

struct MetricCollector {
    values: BTreeMap<String, String>,
    rejection: Option<String>,
}

extern "C" fn metric_publish(ctx: *mut c_void, name: CStr, value: f64) -> i32 {
    let Some(collector) = (unsafe { (ctx as *mut MetricCollector).as_mut() }) else {
        return METRIC_REJECTED;
    };
    let Some(name) = (unsafe { name.as_str() }) else {
        collector.rejection = Some("metric name must be valid UTF-8".to_string());
        return METRIC_INVALID_NAME;
    };
    if !valid_metric_name(name) {
        collector.rejection =
            Some("metric name must match ^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$".to_string());
        return METRIC_INVALID_NAME;
    }
    if RESERVED_METRIC_NAMES.contains(&name) {
        collector.rejection = Some(format!("metric name {name:?} is reserved by the harness"));
        return METRIC_RESERVED_NAME;
    }
    if !value.is_finite() {
        collector.rejection = Some("metric value must be finite".to_string());
        return METRIC_NOT_FINITE;
    }
    collector.values.insert(name.to_string(), value.to_string());
    METRIC_OK
}

fn valid_metric_name(name: &str) -> bool {
    let mut characters = name.chars();
    let Some(first) = characters.next() else {
        return false;
    };
    name.chars().count() <= 128
        && first.is_ascii_alphanumeric()
        && characters.all(|character| {
            character.is_ascii_alphanumeric()
                || matches!(character, '.' | '_' | ':' | '@' | '/' | '-')
        })
}

struct OrderCollector {
    orders: Vec<EngineOrder>,
    rejection: Option<String>,
}

extern "C" fn order_submit(ctx: *mut c_void, order: *const CMarketOrder) -> i32 {
    let Some(collector) = (unsafe { (ctx as *mut OrderCollector).as_mut() }) else {
        return ORDER_REJECTED;
    };
    let Some(raw) = (unsafe { order.as_ref() }) else {
        collector.rejection = Some("submitted order intent is null".to_string());
        return ORDER_REJECTED;
    };
    let strings = (
        unsafe { raw.order_id.as_str() },
        unsafe { raw.symbol.as_str() },
        unsafe { raw.side.as_str() },
        unsafe { raw.order_type.as_str() },
        unsafe { raw.time_in_force.as_str() },
        unsafe { raw.event_id.as_str() },
    );
    let (order_id, symbol, side, order_type, time_in_force, event_id) = match strings {
        (
            Some(order_id),
            Some(symbol),
            Some(side),
            Some(order_type),
            Some(time_in_force),
            Some(event_id),
        ) => (
            order_id.to_string(),
            symbol.to_string(),
            side.to_string(),
            order_type.to_string(),
            time_in_force.to_string(),
            event_id.to_string(),
        ),
        _ => {
            collector.rejection = Some("submitted order intent is malformed".to_string());
            return ORDER_REJECTED;
        }
    };
    collector.orders.push(EngineOrder {
        order_id,
        symbol,
        side,
        quantity: raw.quantity.to_decimal(),
        order_type,
        time_in_force,
        limit_price: raw.has_limit_price.then(|| raw.limit_price.to_decimal()),
        stop_price: raw.has_stop_price.then(|| raw.stop_price.to_decimal()),
        timestamp_ns: 0,
        sequence: raw.sequence,
        event_id,
    });
    SDK_OK
}

/// A JIT-loaded study program driven through the engine seam.
pub struct JitStudyRuntime {
    loaded: LoadedStudy,
    subscriptions: Vec<MarketSubscription>,
    metrics: BTreeMap<String, String>,
}

impl JitStudyRuntime {
    /// Compile, load, and construct one study runtime from authored source.
    ///
    /// # Errors
    ///
    /// Returns [`JitError`] when compilation, loading, or program creation
    /// fails.
    pub fn from_source(
        workspace: &ManagedWorkspace,
        source: &str,
        parameters: &serde_json::Value,
    ) -> Result<Self, JitError> {
        let artifact = compile(workspace, source, ProgramKind::Study)?;
        let loaded = unsafe { LoadedStudy::load(&artifact.artifact_path, parameters)? };
        let subscriptions =
            read_subscriptions(unsafe { (loaded.vtable().subscriptions)(loaded.state()) })?;
        Ok(Self {
            loaded,
            subscriptions,
            metrics: BTreeMap::new(),
        })
    }

    /// The recorded last error from the artifact, if any.
    #[must_use]
    pub fn last_error(&self) -> Option<String> {
        unsafe { self.loaded.last_error() }
    }

    /// Fold one engine frame into the authored program in exactly one callback.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::StrategyExecution`] when the authored callback
    /// fails, panics, or reports malformed frame data.
    pub fn on_bar_frame(&mut self, frame: &EngineBarFrame) -> Result<(), BacktestError> {
        let vtable = self.loaded.vtable();
        let (bars, missing) = frame_storage(frame)?;
        let view = frame_view(frame, &bars, &missing);
        let code = unsafe { (vtable.on_bar_frame)(self.loaded.state(), &view) };
        if code != SDK_OK {
            return Err(BacktestError::StrategyExecution(study_failure(
                self,
                "study bar frame callback failed",
            )));
        }
        Ok(())
    }
}

impl StrategyRuntime for JitStudyRuntime {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(self.subscriptions.clone())
    }
    fn on_start(&mut self, _start: &StrategyStart) -> Result<(), BacktestError> {
        // Study programs are constructed with their parameters at load time;
        // there is no per-run start callback, matching the Python study
        // program contract.
        Ok(())
    }

    fn on_market(&mut self, event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        let vtable = self.loaded.vtable();
        let code = match event {
            EngineMarket::Quote { .. } => unsafe {
                (vtable.on_quote)(self.loaded.state(), &quote_event(event))
            },
            EngineMarket::Trade { .. } => unsafe {
                (vtable.on_trade)(self.loaded.state(), &trade_event(event))
            },
            EngineMarket::Bar(_) => {
                return Err(BacktestError::StrategyExecution(STUDY_BARS_ONLY.into()))
            }
        };
        if code != SDK_OK {
            return Err(BacktestError::StrategyExecution(study_failure(
                self,
                "study market callback failed",
            )));
        }
        Ok(Vec::new())
    }

    fn on_bar(&mut self, _bar: &EngineBar) -> Result<Vec<EngineOrder>, BacktestError> {
        Err(BacktestError::StrategyExecution(
            STUDY_BARS_ONLY.to_string(),
        ))
    }

    fn on_bar_frame(&mut self, frame: &EngineBarFrame) -> Result<(), BacktestError> {
        JitStudyRuntime::on_bar_frame(self, frame)
    }

    fn on_fill(&mut self, _fill: &EngineFill) -> Result<(), BacktestError> {
        // A study observes no execution.
        Ok(())
    }

    fn on_stop(&mut self) -> Result<(), BacktestError> {
        let vtable = self.loaded.vtable();
        let mut collector = MetricCollector {
            values: BTreeMap::new(),
            rejection: None,
        };
        let sink = CMetricSink {
            ctx: &mut collector as *mut MetricCollector as *mut c_void,
            publish: metric_publish as MetricPublishFn,
        };
        let code = unsafe { (vtable.on_stop)(self.loaded.state(), &sink) };
        if code != SDK_OK || collector.rejection.is_some() {
            let detail = collector
                .rejection
                .or_else(|| self.last_error())
                .unwrap_or_else(|| "study stop failed".to_string());
            return Err(BacktestError::StrategyExecution(detail));
        }
        self.metrics = collector.values;
        Ok(())
    }

    fn published_metrics(&self) -> BTreeMap<String, String> {
        self.metrics.clone()
    }
}

/// A JIT-loaded strategy program driven through the engine seam.
pub struct JitStrategyRuntime {
    loaded: LoadedStrategy,
    subscriptions: Vec<MarketSubscription>,
    metrics: BTreeMap<String, String>,
}

impl JitStrategyRuntime {
    /// Compile, load, and construct one strategy runtime from authored source.
    ///
    /// # Errors
    ///
    /// Returns [`JitError`] when compilation, loading, or program creation
    /// fails.
    pub fn from_source(
        workspace: &ManagedWorkspace,
        source: &str,
        parameters: &serde_json::Value,
    ) -> Result<Self, JitError> {
        let artifact = compile(workspace, source, ProgramKind::Strategy)?;
        let loaded = unsafe { LoadedStrategy::load(&artifact.artifact_path, parameters)? };
        let subscriptions =
            read_subscriptions(unsafe { (loaded.vtable().subscriptions)(loaded.state()) })?;
        Ok(Self {
            loaded,
            subscriptions,
            metrics: BTreeMap::new(),
        })
    }

    /// The recorded last error from the artifact, if any.
    #[must_use]
    pub fn last_error(&self) -> Option<String> {
        unsafe { self.loaded.last_error() }
    }
}

impl StrategyRuntime for JitStrategyRuntime {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(self.subscriptions.clone())
    }

    fn on_bar_frame(&mut self, _frame: &EngineBarFrame) -> Result<(), BacktestError> {
        Err(BacktestError::StrategyExecution(
            "strategy frames require on_frame_orders; observation-only dispatch would discard orders".into(),
        ))
    }

    fn on_frame_orders(
        &mut self,
        frame: &EngineBarFrame,
    ) -> Result<Vec<EngineOrder>, BacktestError> {
        let (bars, missing) = frame_storage(frame)?;
        let view = frame_view(frame, &bars, &missing);
        let mut collector = OrderCollector {
            orders: Vec::new(),
            rejection: None,
        };
        let sink = COrderSink {
            ctx: (&mut collector as *mut OrderCollector).cast(),
            submit: order_submit,
        };
        let code =
            unsafe { (self.loaded.vtable().on_bar_frame)(self.loaded.state(), &view, &sink) };
        if code != SDK_OK || collector.rejection.is_some() {
            return Err(BacktestError::StrategyExecution(
                collector
                    .rejection
                    .or_else(|| self.last_error())
                    .unwrap_or_else(|| "strategy frame callback failed".into()),
            ));
        }
        for order in &mut collector.orders {
            order.timestamp_ns = frame.available_at_ns;
        }
        Ok(collector.orders)
    }
    fn on_start(&mut self, start: &StrategyStart) -> Result<(), BacktestError> {
        let vtable = self.loaded.vtable();
        let symbols: Vec<CStr> = start
            .symbols
            .iter()
            .map(|symbol| CStr::from_slice(symbol))
            .collect();
        let start_view = CStart {
            instance_id: CStr::from_slice(&start.instance_id),
            account_id: CStr::from_slice(&start.account_id),
            symbols: harness_strategy_sdk::CSlice {
                ptr: symbols.as_ptr(),
                len: symbols.len(),
            },
            seed: start.seed,
            initial_cash: CDecimal::from_decimal(start.initial_cash),
            start_ns: start.start_ns,
        };
        let code = unsafe { (vtable.on_start)(self.loaded.state(), &start_view) };
        if code != SDK_OK {
            return Err(BacktestError::StrategyExecution(strategy_failure(
                self,
                "strategy start failed",
            )));
        }
        Ok(())
    }

    fn on_market(&mut self, event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        let vtable = self.loaded.vtable();
        let mut collector = OrderCollector {
            orders: Vec::new(),
            rejection: None,
        };
        let sink = COrderSink {
            ctx: &mut collector as *mut OrderCollector as *mut c_void,
            submit: order_submit as OrderSubmitFn,
        };
        let timestamp_ns = event.timestamp_ns();
        let code = match event {
            EngineMarket::Quote { .. } => {
                let quote = quote_event(event);
                unsafe { (vtable.on_quote)(self.loaded.state(), &quote, &sink) }
            }
            EngineMarket::Trade { .. } => {
                let trade = trade_event(event);
                unsafe { (vtable.on_trade)(self.loaded.state(), &trade, &sink) }
            }
            EngineMarket::Bar(_) => {
                return Err(BacktestError::StrategyExecution(
                    "bar events are not part of the strategy contract".to_string(),
                ))
            }
        };
        if code != SDK_OK || collector.rejection.is_some() {
            let detail = collector
                .rejection
                .or_else(|| self.last_error())
                .unwrap_or_else(|| "strategy event callback failed".to_string());
            return Err(BacktestError::StrategyExecution(detail));
        }
        // The host stamps the event time, exactly as the Python adapter does.
        for order in &mut collector.orders {
            order.timestamp_ns = timestamp_ns;
        }
        Ok(collector.orders)
    }

    fn on_fill(&mut self, fill: &EngineFill) -> Result<(), BacktestError> {
        let vtable = self.loaded.vtable();
        let view = CFill {
            event_id: CStr::from_slice(&fill.event_id),
            timestamp_ns: fill.timestamp_ns,
            sequence: fill.sequence,
            fill_id: CStr::from_slice(&fill.fill_id),
            order_id: CStr::from_slice(&fill.order_id),
            symbol: CStr::from_slice(&fill.symbol),
            side: CStr::from_slice(&fill.side),
            quantity: CDecimal::from_decimal(fill.quantity),
            price: CDecimal::from_decimal(fill.price),
            fee: CDecimal::from_decimal(fill.fee),
            liquidity: CStr::from_slice(&fill.liquidity),
        };
        let code = unsafe { (vtable.on_fill)(self.loaded.state(), &view) };
        if code != SDK_OK {
            return Err(BacktestError::StrategyExecution(strategy_failure(
                self,
                "strategy fill callback failed",
            )));
        }
        Ok(())
    }

    fn on_stop(&mut self) -> Result<(), BacktestError> {
        let vtable = self.loaded.vtable();
        let mut collector = MetricCollector {
            values: BTreeMap::new(),
            rejection: None,
        };
        let sink = CMetricSink {
            ctx: &mut collector as *mut MetricCollector as *mut c_void,
            publish: metric_publish as MetricPublishFn,
        };
        let code = unsafe { (vtable.on_stop)(self.loaded.state(), &sink) };
        if code != SDK_OK || collector.rejection.is_some() {
            let detail = collector
                .rejection
                .or_else(|| self.last_error())
                .unwrap_or_else(|| "strategy stop failed".to_string());
            return Err(BacktestError::StrategyExecution(detail));
        }
        self.metrics = collector.values;
        Ok(())
    }

    fn published_metrics(&self) -> BTreeMap<String, String> {
        self.metrics.clone()
    }
}

/// Build one engine strategy instance around a JIT-loaded study program.
///
/// # Errors
///
/// Returns [`JitError`] when compilation, loading, or program creation fails.
pub fn study_instance(
    workspace: &ManagedWorkspace,
    source: &str,
    parameters: &serde_json::Value,
    instance_id: &str,
) -> Result<StrategyInstance, JitError> {
    let runtime = JitStudyRuntime::from_source(workspace, source, parameters)?;
    Ok(StrategyInstance {
        instance_id: instance_id.to_string(),
        runtime: Box::new(runtime),
    })
}

/// Build one engine strategy instance around a JIT-loaded strategy program.
///
/// # Errors
///
/// Returns [`JitError`] when compilation, loading, or program creation fails.
pub fn strategy_instance(
    workspace: &ManagedWorkspace,
    source: &str,
    parameters: &serde_json::Value,
    instance_id: &str,
) -> Result<StrategyInstance, JitError> {
    let runtime = JitStrategyRuntime::from_source(workspace, source, parameters)?;
    Ok(StrategyInstance {
        instance_id: instance_id.to_string(),
        runtime: Box::new(runtime),
    })
}

fn study_failure(runtime: &JitStudyRuntime, prefix: &str) -> String {
    runtime.last_error().map_or_else(
        || prefix.to_string(),
        |detail| format!("{prefix}: {detail}"),
    )
}

fn strategy_failure(runtime: &JitStrategyRuntime, prefix: &str) -> String {
    runtime.last_error().map_or_else(
        || prefix.to_string(),
        |detail| format!("{prefix}: {detail}"),
    )
}

fn quote_event(event: &EngineMarket) -> CQuoteEvent {
    let EngineMarket::Quote {
        event_id,
        timestamp_ns,
        sequence,
        symbol,
        bid,
        ask,
        bid_quantity,
        ask_quantity,
        venue,
    } = event
    else {
        unreachable!("quote_event called with a trade event");
    };
    CQuoteEvent {
        event_id: CStr::from_slice(event_id),
        timestamp_ns: *timestamp_ns,
        sequence: *sequence,
        symbol: CStr::from_slice(symbol),
        bid: CDecimal::from_decimal(*bid),
        ask: CDecimal::from_decimal(*ask),
        bid_quantity: CDecimal::from_decimal(*bid_quantity),
        ask_quantity: CDecimal::from_decimal(*ask_quantity),
        venue: CStr::from_slice(venue),
    }
}

fn trade_event(event: &EngineMarket) -> CTradeEvent {
    let EngineMarket::Trade {
        event_id,
        timestamp_ns,
        sequence,
        symbol,
        price,
        quantity,
        venue,
        trade_id,
    } = event
    else {
        unreachable!("trade_event called with a quote event");
    };
    CTradeEvent {
        event_id: CStr::from_slice(event_id),
        timestamp_ns: *timestamp_ns,
        sequence: *sequence,
        symbol: CStr::from_slice(symbol),
        price: CDecimal::from_decimal(*price),
        quantity: CDecimal::from_decimal(*quantity),
        venue: CStr::from_slice(venue),
        trade_id: CStr::from_slice(trade_id),
    }
}
