//! Engine runtime adapters over loaded authored programs.
//!
//! These implement the engine's `StrategyRuntime` seam with the exact
//! capability split the Python adapters established: a study runtime folds
//! quotes, trades, and atomic bar frames and never returns orders; a strategy submits
//! intents the host stamps with the event time, and every metric publish is
//! validated host-side so a rejected metric fails the run.

use std::collections::BTreeMap;
use std::os::raw::c_void;

use harness_contracts::causal::CaptureRequest;
use harness_strategy_sdk::{CDecisionSink, DecisionFn, StudyOutput};
use harness_trading::causal::ObservationLedger;

use harness_strategy_sdk::canonical_subscriptions;
use harness_strategy_sdk::CBarEvent;
use harness_strategy_sdk::CBarFrame;
use harness_strategy_sdk::CDecimal;
use harness_strategy_sdk::CExecutionSource;
use harness_strategy_sdk::CFill;
use harness_strategy_sdk::CMarketOrder;
use harness_strategy_sdk::CMetricSink;
use harness_strategy_sdk::COrderSink;
use harness_strategy_sdk::CQuoteEvent;
use harness_strategy_sdk::CStart;
use harness_strategy_sdk::CStr;
use harness_strategy_sdk::CTradeEvent;
use harness_strategy_sdk::MetricPublishFn;
use harness_strategy_sdk::OrderSubmitFn;
use harness_strategy_sdk::Subscription;
use harness_strategy_sdk::METRIC_INVALID_NAME;
use harness_strategy_sdk::METRIC_NOT_FINITE;
use harness_strategy_sdk::METRIC_OK;
use harness_strategy_sdk::METRIC_RESERVED_NAME;
use harness_strategy_sdk::SDK_OK;
use harness_trading::program::EngineBar;
use harness_trading::program::EngineBarFrame;
use harness_trading::program::EngineFill;
use harness_trading::program::EngineMarket;
use harness_trading::program::EngineOrder;
use harness_trading::program::MarketSubscription;
use harness_trading::program::StrategyInstance;
use harness_trading::program::StrategyRuntime;
use harness_trading::program::StrategyStart;
use harness_trading::replay_errors::BacktestError;

use crate::compile::compile;
use crate::compile::ProgramKind;
use crate::error::JitError;
use crate::load::LoadedStrategy;
use crate::load::LoadedStudy;
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

pub fn engine_subscription(input: Subscription) -> MarketSubscription {
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
    outputs: BTreeMap<String, serde_json::Value>,
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
        collector.rejection = Some(format!(
            "metric name {name:?} must match ^[A-Za-z0-9][A-Za-z0-9._:@/-]{{0,127}}$"
        ));
        return METRIC_INVALID_NAME;
    }
    if harness_strategy_sdk::is_reserved_metric_name(name) {
        collector.rejection = Some(format!("metric name {name:?} is reserved by the harness"));
        return METRIC_RESERVED_NAME;
    }
    if !value.is_finite() {
        collector.rejection = Some(format!("metric {name:?}: value {value:?} must be finite; omit an undefined metric or publish an explicit undefined observation"));
        return METRIC_NOT_FINITE;
    }
    collector.values.insert(name.to_string(), value.to_string());
    METRIC_OK
}

unsafe extern "C" fn output_publish(ctx: *mut c_void, name: CStr, json: CStr) -> i32 {
    let Some(collector) = (unsafe { (ctx as *mut MetricCollector).as_mut() }) else {
        return METRIC_REJECTED;
    };
    let parsed = (|| -> Result<(String, serde_json::Value), String> {
        let name = unsafe { name.as_str() }.ok_or("output name must be UTF-8")?;
        if !valid_metric_name(name) {
            return Err(format!(
                "output name {name:?} must match ^[A-Za-z0-9][A-Za-z0-9._:@/-]{{0,127}}$"
            ));
        }
        if name.starts_with("causal/") {
            return Err("causal/ is reserved for host-sealed replay observations".into());
        }
        let json = unsafe { json.as_str() }.ok_or("output JSON must be UTF-8")?;
        let document: serde_json::Value = serde_json::from_str(json).map_err(|e| e.to_string())?;
        let output: harness_strategy_sdk::StudyOutput = serde_path_to_error::deserialize(document)
            .map_err(|e| format!("output {name:?}: {e}"))?;
        if matches!(output, StudyOutput::CausalObservations { .. }) {
            return Err("only the host may publish causal observations".into());
        }
        output
            .validate()
            .map_err(|e| format!("output {name:?}: {e}"))?;
        if collector.outputs.contains_key(name) {
            return Err(format!(
                "output {name:?}: duplicate publication; publish each output name once"
            ));
        }
        Ok((
            name.into(),
            serde_json::to_value(output).map_err(|e| e.to_string())?,
        ))
    })();
    match parsed {
        Ok((name, value)) => {
            collector.outputs.insert(name, value);
            METRIC_OK
        }
        Err(error) => {
            collector.rejection = Some(error);
            METRIC_REJECTED
        }
    }
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

struct DecisionCollector<'a> {
    ledger: &'a mut ObservationLedger,
    frame: &'a EngineBarFrame,
    rejection: Option<String>,
}

unsafe extern "C" fn capture_observation(ctx: *mut c_void, request: CStr) -> i32 {
    let collector = &mut *ctx.cast::<DecisionCollector<'_>>();
    let result = (|| {
        let json = request.as_str().ok_or("capture JSON must be UTF-8")?;
        let request: CaptureRequest =
            serde_path_to_error::deserialize(&mut serde_json::Deserializer::from_str(json))
                .map_err(|error| format!("capture request: {error}"))?;
        let location = format!(
            "dataset={:?}, symbol={:?}, interval={}, decision_at_ns={}",
            request.dataset,
            request.symbol,
            collector.frame.interval,
            collector.frame.available_at_ns
        );
        collector
            .ledger
            .capture(collector.frame, request)
            .map_err(|error| format!("{location}: {error}"))
    })();
    match result {
        Ok(()) => SDK_OK,
        Err(error) => {
            collector.rejection = Some(format!("causal capture rejected: {error}"));
            METRIC_REJECTED
        }
    }
}

fn seal_decision(
    ledger: &mut ObservationLedger,
    frame: &EngineBarFrame,
    state: *mut c_void,
    callback: DecisionFn,
) -> Result<(), BacktestError> {
    let mut collector = DecisionCollector {
        ledger,
        frame,
        rejection: None,
    };
    let sink = CDecisionSink {
        ctx: (&mut collector as *mut DecisionCollector<'_>).cast(),
        decision_at_ns: frame.available_at_ns,
        interval: CStr::from_slice(&frame.interval),
        capture: capture_observation,
    };
    let code = unsafe { callback(state, &sink) };
    if code != SDK_OK || collector.rejection.is_some() {
        return Err(BacktestError::StrategyExecution(
            collector
                .rejection
                .unwrap_or_else(|| "causal decision callback failed".into()),
        ));
    }
    Ok(())
}

fn publish_observations(
    ledger: &ObservationLedger,
    outputs: &mut BTreeMap<String, serde_json::Value>,
) -> Result<(), BacktestError> {
    for (name, dataset) in ledger.outputs() {
        dataset
            .validate()
            .map_err(BacktestError::StrategyExecution)?;
        let output = serde_json::to_value(StudyOutput::CausalObservations { dataset })
            .map_err(|error| BacktestError::StrategyExecution(error.to_string()))?;
        outputs.insert(name, output);
    }
    Ok(())
}

struct OrderCollector {
    actions: Vec<harness_trading::program::OrderAction>,
    rejection: Option<String>,
    next_id: u64,
    instance_id: String,
    generated_id: String,
}

/// Caches serialization on first access. History caches survive callbacks while
/// their immutable backing arrays are unchanged; account marks never go stale.
#[derive(Default)]
struct ExecutionViews {
    snapshot: Option<harness_trading::program::ExecutionSnapshot>,
    account: std::cell::OnceCell<String>,
    orders: std::cell::OnceCell<String>,
    trades: std::cell::OnceCell<String>,
}
impl ExecutionViews {
    fn update(&mut self, snapshot: &harness_trading::program::ExecutionSnapshot) {
        if self
            .snapshot
            .as_ref()
            .is_none_or(|previous| !std::sync::Arc::ptr_eq(&previous.orders, &snapshot.orders))
        {
            self.orders.take();
        }
        if self.snapshot.as_ref().is_none_or(|previous| {
            !std::sync::Arc::ptr_eq(&previous.closed_trades, &snapshot.closed_trades)
        }) {
            self.trades.take();
        }
        self.account.take();
        self.snapshot = Some(snapshot.clone());
    }
    fn source(&self) -> CExecutionSource {
        CExecutionSource {
            ctx: (self as *const Self).cast(),
            read: read_execution,
        }
    }
}
unsafe extern "C" fn read_execution(ctx: *const c_void, view: u32) -> CStr {
    let views = &*ctx.cast::<ExecutionViews>();
    let Some(snapshot) = &views.snapshot else {
        return CStr::from_slice(if view == 0 { "" } else { "[]" });
    };
    let value = match view {
        0 => views
            .account
            .get_or_init(|| snapshot.account_json().to_string()),
        1 => views
            .orders
            .get_or_init(|| snapshot.orders_json().to_string()),
        2 => views
            .trades
            .get_or_init(|| snapshot.closed_trades_json().to_string()),
        _ => return CStr::from_slice(""),
    };
    CStr::from_slice(value)
}

#[cfg(test)]
#[path = "../tests/unit/execution_views.rs"]
mod execution_views_tests;

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
    collector
        .actions
        .push(harness_trading::program::OrderAction::Submit(EngineOrder {
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
        }));
    SDK_OK
}

unsafe extern "C" fn next_order_id(ctx: *mut c_void) -> CStr {
    let collector = &mut *(ctx as *mut OrderCollector);
    collector.next_id += 1;
    collector.generated_id = format!("sdk:{}:{}", collector.instance_id, collector.next_id);
    CStr::from_slice(&collector.generated_id)
}
unsafe extern "C" fn cancel_order(ctx: *mut c_void, order_id: CStr) -> i32 {
    let collector = &mut *(ctx as *mut OrderCollector);
    let Some(order_id) = order_id.as_str().filter(|id| !id.is_empty()) else {
        collector.rejection =
            Some("cancel.order_id: expected a nonempty UTF-8 owned order ID".into());
        return ORDER_REJECTED;
    };
    collector
        .actions
        .push(harness_trading::program::OrderAction::Cancel {
            order_id: order_id.into(),
        });
    SDK_OK
}
unsafe extern "C" fn target_position(ctx: *mut c_void, symbol: CStr, quantity: CDecimal) -> i32 {
    let collector = &mut *(ctx as *mut OrderCollector);
    let Some(symbol) = symbol.as_str().filter(|symbol| !symbol.is_empty()) else {
        collector.rejection =
            Some("target_position.symbol: expected a nonempty UTF-8 subscribed symbol".into());
        return ORDER_REJECTED;
    };
    collector
        .actions
        .push(harness_trading::program::OrderAction::TargetPosition {
            symbol: symbol.into(),
            quantity: quantity.to_decimal(),
        });
    SDK_OK
}
fn order_only(
    actions: Vec<harness_trading::program::OrderAction>,
) -> Result<Vec<EngineOrder>, BacktestError> {
    actions
        .into_iter()
        .map(|action| match action {
            harness_trading::program::OrderAction::Submit(order) => Ok(order),
            _ => Err(BacktestError::StrategyExecution(
                "managed execution actions require the engine action entrypoint".into(),
            )),
        })
        .collect()
}

/// A JIT-loaded study program driven through the engine seam.
pub struct JitStudyRuntime {
    observations: ObservationLedger,
    loaded: LoadedStudy,
    subscriptions: Vec<MarketSubscription>,
    metrics: BTreeMap<String, String>,
    outputs: BTreeMap<String, serde_json::Value>,
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
            observations: ObservationLedger::default(),
            loaded,
            subscriptions,
            metrics: BTreeMap::new(),
            outputs: BTreeMap::new(),
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
        self.observations
            .advance(frame)
            .map_err(BacktestError::StrategyExecution)?;
        let code = unsafe { (vtable.on_bar_frame)(self.loaded.state(), &view) };
        if code != SDK_OK {
            return Err(BacktestError::StrategyExecution(study_failure(
                self,
                "study bar frame callback failed",
            )));
        }
        seal_decision(
            &mut self.observations,
            frame,
            self.loaded.state(),
            self.loaded.vtable().on_decision,
        )
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
        let code = match event.data_type() {
            "quote" => unsafe { (vtable.on_quote)(self.loaded.state(), &quote_event(event)) },
            "trade" => unsafe { (vtable.on_trade)(self.loaded.state(), &trade_event(event)) },
            _ => return Err(BacktestError::StrategyExecution(STUDY_BARS_ONLY.into())),
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
            outputs: BTreeMap::new(),
            rejection: None,
        };
        let empty_execution = ExecutionViews::default();
        let sink = CMetricSink {
            ctx: &mut collector as *mut MetricCollector as *mut c_void,
            publish: metric_publish as MetricPublishFn,
            publish_output: output_publish,
            execution: empty_execution.source(),
        };
        let code = unsafe { (vtable.on_stop)(self.loaded.state(), &sink) };
        if code != SDK_OK || collector.rejection.is_some() {
            let detail = collector
                .rejection
                .or_else(|| self.last_error())
                .unwrap_or_else(|| "study stop failed".to_string());
            return Err(BacktestError::StrategyExecution(detail));
        }
        publish_observations(&self.observations, &mut collector.outputs)?;
        self.metrics = collector.values;
        harness_strategy_sdk::research::validate_publication(&collector.outputs)
            .map_err(BacktestError::StrategyExecution)?;
        self.outputs = collector.outputs;
        Ok(())
    }

    fn published_metrics(&self) -> BTreeMap<String, String> {
        self.metrics.clone()
    }
    fn published_outputs(&self) -> BTreeMap<String, serde_json::Value> {
        self.outputs.clone()
    }
}

/// A JIT-loaded strategy program driven through the engine seam.
pub struct JitStrategyRuntime {
    observations: ObservationLedger,
    loaded: LoadedStrategy,
    subscriptions: Vec<MarketSubscription>,
    metrics: BTreeMap<String, String>,
    outputs: BTreeMap<String, serde_json::Value>,
    execution: ExecutionViews,
    next_order_id: u64,
    instance_id: String,
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
            observations: ObservationLedger::default(),
            execution: ExecutionViews::default(),
            next_order_id: 0,
            instance_id: String::new(),
            loaded,
            subscriptions,
            metrics: BTreeMap::new(),
            outputs: BTreeMap::new(),
        })
    }

    /// The recorded last error from the artifact, if any.
    #[must_use]
    pub fn last_error(&self) -> Option<String> {
        unsafe { self.loaded.last_error() }
    }
}

impl StrategyRuntime for JitStrategyRuntime {
    fn wants_execution_state(&self) -> bool {
        true
    }
    fn update_execution_state(
        &mut self,
        snapshot: &harness_trading::program::ExecutionSnapshot,
    ) -> Result<(), BacktestError> {
        self.execution.update(snapshot);
        Ok(())
    }
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(self.subscriptions.clone())
    }

    fn on_bar_frame(&mut self, _frame: &EngineBarFrame) -> Result<(), BacktestError> {
        Err(BacktestError::StrategyExecution(
            "strategy frames require on_frame_orders; observation-only dispatch would discard orders".into(),
        ))
    }

    fn on_frame_actions(
        &mut self,
        frame: &EngineBarFrame,
    ) -> Result<Vec<harness_trading::program::OrderAction>, BacktestError> {
        let (bars, missing) = frame_storage(frame)?;
        let view = frame_view(frame, &bars, &missing);
        self.observations
            .advance(frame)
            .map_err(BacktestError::StrategyExecution)?;
        let mut collector = OrderCollector {
            actions: Vec::new(),
            rejection: None,
            next_id: self.next_order_id,
            instance_id: self.instance_id.clone(),
            generated_id: String::new(),
        };
        let sink = COrderSink {
            ctx: (&mut collector as *mut OrderCollector).cast(),
            submit: order_submit,
            execution: self.execution.source(),
            next_order_id,
            cancel: cancel_order,
            target_position,
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
        seal_decision(
            &mut self.observations,
            frame,
            self.loaded.state(),
            self.loaded.vtable().on_decision,
        )?;
        self.next_order_id = collector.next_id;
        for action in &mut collector.actions {
            if let harness_trading::program::OrderAction::Submit(order) = action {
                order.timestamp_ns = frame.available_at_ns;
            }
        }
        Ok(collector.actions)
    }
    fn on_frame_orders(
        &mut self,
        frame: &EngineBarFrame,
    ) -> Result<Vec<EngineOrder>, BacktestError> {
        order_only(self.on_frame_actions(frame)?)
    }
    fn on_start(&mut self, start: &StrategyStart) -> Result<(), BacktestError> {
        self.instance_id = start.instance_id.clone();
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
        order_only(self.on_market_actions(event)?)
    }
    fn on_market_actions(
        &mut self,
        event: &EngineMarket,
    ) -> Result<Vec<harness_trading::program::OrderAction>, BacktestError> {
        let vtable = self.loaded.vtable();
        let mut collector = OrderCollector {
            actions: Vec::new(),
            rejection: None,
            next_id: self.next_order_id,
            instance_id: self.instance_id.clone(),
            generated_id: String::new(),
        };
        let sink = COrderSink {
            ctx: &mut collector as *mut OrderCollector as *mut c_void,
            submit: order_submit as OrderSubmitFn,
            execution: self.execution.source(),
            next_order_id,
            cancel: cancel_order,
            target_position,
        };
        let timestamp_ns = event.timestamp_ns();
        let code = match event.data_type() {
            "quote" => {
                let quote = quote_event(event);
                unsafe { (vtable.on_quote)(self.loaded.state(), &quote, &sink) }
            }
            "trade" => {
                let trade = trade_event(event);
                unsafe { (vtable.on_trade)(self.loaded.state(), &trade, &sink) }
            }
            _ => {
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
        self.next_order_id = collector.next_id;
        for action in &mut collector.actions {
            if let harness_trading::program::OrderAction::Submit(order) = action {
                order.timestamp_ns = timestamp_ns;
            }
        }
        Ok(collector.actions)
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
            outputs: BTreeMap::new(),
            rejection: None,
        };
        let sink = CMetricSink {
            ctx: &mut collector as *mut MetricCollector as *mut c_void,
            publish: metric_publish as MetricPublishFn,
            publish_output: output_publish,
            execution: self.execution.source(),
        };
        let code = unsafe { (vtable.on_stop)(self.loaded.state(), &sink) };
        if code != SDK_OK || collector.rejection.is_some() {
            let detail = collector
                .rejection
                .or_else(|| self.last_error())
                .unwrap_or_else(|| "strategy stop failed".to_string());
            return Err(BacktestError::StrategyExecution(detail));
        }
        publish_observations(&self.observations, &mut collector.outputs)?;
        self.metrics = collector.values;
        harness_strategy_sdk::research::validate_publication(&collector.outputs)
            .map_err(BacktestError::StrategyExecution)?;
        self.outputs = collector.outputs;
        Ok(())
    }

    fn published_metrics(&self) -> BTreeMap<String, String> {
        self.metrics.clone()
    }
    fn published_outputs(&self) -> BTreeMap<String, serde_json::Value> {
        self.outputs.clone()
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
    let harness_trading::market::MarketView::Quote {
        event_id,
        timestamp_ns,
        sequence,
        symbol,
        bid,
        ask,
        bid_quantity,
        ask_quantity,
        venue,
    } = event.view()
    else {
        unreachable!("quote_event called with a trade event");
    };
    CQuoteEvent {
        event_id: CStr::from_slice(event_id),
        timestamp_ns,
        sequence,
        symbol: CStr::from_slice(symbol),
        bid: CDecimal::from_decimal(bid),
        ask: CDecimal::from_decimal(ask),
        bid_quantity: CDecimal::from_decimal(bid_quantity),
        ask_quantity: CDecimal::from_decimal(ask_quantity),
        venue: CStr::from_slice(venue),
    }
}

fn trade_event(event: &EngineMarket) -> CTradeEvent {
    let harness_trading::market::MarketView::Trade {
        event_id,
        timestamp_ns,
        sequence,
        symbol,
        price,
        quantity,
        venue,
        trade_id,
    } = event.view()
    else {
        unreachable!("trade_event called with a quote event");
    };
    CTradeEvent {
        event_id: CStr::from_slice(event_id),
        timestamp_ns,
        sequence,
        symbol: CStr::from_slice(symbol),
        price: CDecimal::from_decimal(price),
        quantity: CDecimal::from_decimal(quantity),
        venue: CStr::from_slice(venue),
        trade_id: CStr::from_slice(trade_id),
    }
}
