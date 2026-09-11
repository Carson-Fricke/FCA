//! Native-engine plugin adapter for user-authored Python strategies.
//!
//! [`PyStrategyRuntime`] implements
//! [`backtest::backtest::engine_core::StrategyRuntime`] over an
//! unmodified Python strategy class. The Rust engine owns the clock, event bus,
//! venue, fees, portfolio, and risk; the adapter hands the strategy the native
//! [`PyEngineContext`] (clock, universe, books, state, seeded random, orders,
//! emissions, log) and converts the Python orders it submits back into engine
//! intents.

use std::collections::BTreeMap;
use std::path::PathBuf;

use backtest::backtest::engine_core::{
    EngineFill, EngineMarket, EngineOrder, StrategyRuntime, StrategyStart,
};
use backtest::backtest::errors::BacktestError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::context::{build_context, PyBooks, PyClock, PyEngineContext, PyOrders};
use crate::error::HostError;
use crate::python::{default_search_paths, install_marketdata_shim, install_search_paths};

/// A Python strategy driven through the native engine plugin seam.
pub struct PyStrategyRuntime {
    module: String,
    class: String,
    parameters: BTreeMap<String, serde_json::Value>,
    search_paths: Vec<PathBuf>,
    strategy: Option<Py<PyAny>>,
    context: Option<Py<PyEngineContext>>,
    clock: Option<Py<PyClock>>,
    orders: Option<Py<PyOrders>>,
    books: Option<Py<PyBooks>>,
}

impl PyStrategyRuntime {
    /// Build an unstarted runtime for one configured `module:Class`.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the implementation path is malformed.
    pub fn new(
        implementation: &str,
        parameters: BTreeMap<String, serde_json::Value>,
    ) -> Result<Self, HostError> {
        let (module, class) = implementation.rsplit_once(':').ok_or_else(|| {
            HostError::Python("strategy implementation must use module:Class syntax".to_string())
        })?;
        let mut search_paths = Vec::new();
        if let Some(root) = crate::python::workspace_root() {
            search_paths.push(root);
        }
        search_paths.extend(default_search_paths());
        Ok(Self {
            module: module.to_string(),
            class: class.to_string(),
            parameters,
            search_paths,
            strategy: None,
            context: None,
            clock: None,
            orders: None,
            books: None,
        })
    }

    /// Instantiate the strategy and build its native context.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError`] when the Python strategy raises.
    fn build(&mut self, start: &StrategyStart) -> Result<(), BacktestError> {
        let module = self.module.clone();
        let class = self.class.clone();
        let parameters = self.parameters.clone();
        let search_paths = self.search_paths.clone();
        let symbols = start.symbols.clone();
        let now_ns = start.start_ns;
        let book_id = start.instance_id.clone();
        let initial_cash = start.initial_cash;
        let seed = start.seed;

        type BuiltRuntime = (
            Py<PyAny>,
            Py<PyEngineContext>,
            Py<PyClock>,
            Py<PyOrders>,
            Py<PyBooks>,
        );

        let built: Result<BuiltRuntime, HostError> = Python::attach(|py| {
            install_search_paths(py, &search_paths)?;
            install_marketdata_shim(py)?;
            let (context, clock, orders, books) =
                build_context(py, symbols, now_ns, book_id, initial_cash, seed)?;
            let module_obj = py.import(module.as_str())?;
            let class_obj = module_obj.getattr(class.as_str())?;
            let kwargs = PyDict::new(py);
            for (name, value) in &parameters {
                kwargs.set_item(name, crate::python::json_loads(py, &value.to_string())?)?;
            }
            let instance = match class_obj.call((), Some(&kwargs)) {
                Ok(instance) => instance,
                Err(first_error) => {
                    let fallback = PyDict::new(py);
                    fallback.set_item("parameters", &kwargs)?;
                    class_obj
                        .call((), Some(&fallback))
                        .map_err(|_| first_error)?
                }
            };
            instance.call_method1("on_start", (context.bind(py),))?;
            Ok((instance.unbind(), context, clock, orders, books))
        });
        let (strategy, context, clock, orders, books) = built.map_err(host_to_backtest)?;
        self.strategy = Some(strategy);
        self.context = Some(context);
        self.clock = Some(clock);
        self.orders = Some(orders);
        self.books = Some(books);
        Ok(())
    }
}

impl StrategyRuntime for PyStrategyRuntime {
    fn on_start(&mut self, start: &StrategyStart) -> Result<(), BacktestError> {
        self.build(start)
    }

    fn on_market(&mut self, event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        let strategy = self
            .strategy
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("strategy not started".to_string()))?;
        let clock = self
            .clock
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("clock not built".to_string()))?;
        let orders = self
            .orders
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("orders not built".to_string()))?;
        let books = self
            .books
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("books not built".to_string()))?;
        let timestamp_ns = event.timestamp_ns();
        let result: Result<Vec<EngineOrder>, HostError> = Python::attach(|py| {
            clock.bind(py).borrow().set(timestamp_ns);
            books
                .bind(py)
                .borrow()
                .mark(event.symbol(), event.mark_price());
            orders.bind(py).borrow().clear_pending();
            let callback = match event {
                EngineMarket::Quote { .. } => "on_quote",
                EngineMarket::Trade { .. } => "on_trade",
            };
            let py_event = market_to_python(py, event)?;
            strategy.bind(py).call_method1(callback, (py_event,))?;
            let mut pending = orders.bind(py).borrow().take_pending();
            for order in &mut pending {
                order.timestamp_ns = timestamp_ns;
            }
            Ok(pending)
        });
        result.map_err(host_to_backtest)
    }

    fn on_fill(&mut self, fill: &EngineFill) -> Result<(), BacktestError> {
        let strategy = self
            .strategy
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("strategy not started".to_string()))?;
        let books = self
            .books
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("books not built".to_string()))?;
        let result: Result<(), HostError> = Python::attach(|py| {
            books.bind(py).borrow().apply_fill(
                &fill.side,
                &fill.symbol,
                fill.quantity,
                fill.price,
                fill.fee,
            );
            let py_fill = fill_to_python(py, fill)?;
            strategy.bind(py).call_method1("on_fill", (py_fill,))?;
            Ok(())
        });
        result.map_err(host_to_backtest)
    }

    fn on_stop(&mut self) -> Result<(), BacktestError> {
        let strategy = self
            .strategy
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("strategy not started".to_string()))?;
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("context not built".to_string()))?;
        let result: Result<(), HostError> = Python::attach(|py| {
            strategy
                .bind(py)
                .call_method1("on_stop", (context.bind(py),))?;
            Ok(())
        });
        result.map_err(host_to_backtest)
    }
}

fn host_to_backtest(error: HostError) -> BacktestError {
    BacktestError::StrategyExecution(error.to_string())
}

fn market_to_python<'py>(
    py: Python<'py>,
    event: &EngineMarket,
) -> Result<Bound<'py, PyAny>, HostError> {
    let types = py.import("harness.core.types")?;
    match event {
        EngineMarket::Quote {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            bid,
            ask,
            bid_quantity,
            ask_quantity,
            venue,
        } => {
            let class = types.getattr("QuoteEvent")?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("event_id", event_id)?;
            kwargs.set_item("timestamp", datetime_of(py, *timestamp_ns)?)?;
            kwargs.set_item("sequence", *sequence)?;
            kwargs.set_item("symbol", symbol)?;
            kwargs.set_item("bid_price", bid.to_string())?;
            kwargs.set_item("bid_quantity", bid_quantity.to_string())?;
            kwargs.set_item("ask_price", ask.to_string())?;
            kwargs.set_item("ask_quantity", ask_quantity.to_string())?;
            kwargs.set_item("venue", venue)?;
            Ok(class.call((), Some(&kwargs))?)
        }
        EngineMarket::Trade {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            price,
            quantity,
            venue,
            trade_id,
        } => {
            let class = types.getattr("TradeEvent")?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("event_id", event_id)?;
            kwargs.set_item("timestamp", datetime_of(py, *timestamp_ns)?)?;
            kwargs.set_item("sequence", *sequence)?;
            kwargs.set_item("symbol", symbol)?;
            kwargs.set_item("price", price.to_string())?;
            kwargs.set_item("quantity", quantity.to_string())?;
            kwargs.set_item("venue", venue)?;
            kwargs.set_item("trade_id", trade_id)?;
            Ok(class.call((), Some(&kwargs))?)
        }
    }
}

fn fill_to_python<'py>(py: Python<'py>, fill: &EngineFill) -> Result<Bound<'py, PyAny>, HostError> {
    let types = py.import("harness.core.types")?;
    let class = types.getattr("Fill")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("event_id", &fill.event_id)?;
    kwargs.set_item("timestamp", datetime_of(py, fill.timestamp_ns)?)?;
    kwargs.set_item("sequence", fill.sequence)?;
    kwargs.set_item("fill_id", &fill.fill_id)?;
    kwargs.set_item("order_id", &fill.order_id)?;
    kwargs.set_item("symbol", &fill.symbol)?;
    kwargs.set_item("side", &fill.side)?;
    kwargs.set_item("quantity", fill.quantity.to_string())?;
    kwargs.set_item("price", fill.price.to_string())?;
    kwargs.set_item("fee", fill.fee.to_string())?;
    kwargs.set_item("liquidity", &fill.liquidity)?;
    Ok(class.call((), Some(&kwargs))?)
}

fn datetime_of<'py>(py: Python<'py>, timestamp_ns: i64) -> Result<Bound<'py, PyAny>, HostError> {
    let iso = backtest::backtest::manifest::utc_timestamp(timestamp_ns, false);
    Ok(py
        .import("datetime")?
        .getattr("datetime")?
        .call_method1("fromisoformat", (iso,))?)
}

/// Extract the per-strategy parameters from a configuration JSON object.
#[must_use]
pub fn parameters_from_json(
    parameters: &BTreeMap<String, backtest::canonical_json::JsonValue>,
) -> BTreeMap<String, serde_json::Value> {
    parameters
        .iter()
        .map(|(key, value)| (key.clone(), json_to_serde(value)))
        .collect()
}

fn json_to_serde(value: &backtest::canonical_json::JsonValue) -> serde_json::Value {
    use backtest::canonical_json::JsonValue;
    match value {
        JsonValue::Null => serde_json::Value::Null,
        JsonValue::Bool(inner) => serde_json::Value::Bool(*inner),
        JsonValue::Int(inner) => serde_json::Value::from(*inner),
        JsonValue::UInt(inner) => serde_json::Value::from(*inner),
        JsonValue::String(inner) => serde_json::Value::String(inner.clone()),
        JsonValue::Array(items) => {
            serde_json::Value::Array(items.iter().map(json_to_serde).collect())
        }
        JsonValue::Object(entries) => {
            let mut map = serde_json::Map::new();
            for (key, item) in entries {
                map.insert(key.clone(), json_to_serde(item));
            }
            serde_json::Value::Object(map)
        }
    }
}

/// Build one Python-backed runtime per configured strategy instance.
///
/// # Errors
///
/// Returns [`HostError`] when a strategy implementation path is malformed.
pub fn build_strategy_instances(
    configuration: &backtest::backtest::config::BacktestConfig,
) -> Result<Vec<backtest::backtest::engine_core::StrategyInstance>, HostError> {
    let mut instances = Vec::new();
    for configured in &configuration.strategy_instances {
        let runtime = PyStrategyRuntime::new(
            &configured.implementation,
            parameters_from_json(&configured.parameters),
        )?;
        instances.push(backtest::backtest::engine_core::StrategyInstance {
            instance_id: configured.instance_id.clone(),
            runtime: Box::new(runtime),
        });
    }
    Ok(instances)
}

/// Load, run, and write one native backtest job with Python strategies.
///
/// # Errors
///
/// Returns [`HostError`] when a strategy cannot be built.
pub fn run_backtest_job(
    config_path: &std::path::Path,
    output_dir: Option<&std::path::Path>,
) -> Result<
    (
        backtest::backtest::engine_core::BacktestOutcome,
        std::path::PathBuf,
    ),
    HostError,
> {
    run_backtest_job_with_data_dir(config_path, output_dir, None)
}

/// Load, run, and write one native backtest job over a local Parquet lake.
///
/// # Errors
///
/// Returns [`HostError`] when a strategy cannot be built or the lake directory
/// cannot be read.
pub fn run_backtest_job_with_data_dir(
    config_path: &std::path::Path,
    output_dir: Option<&std::path::Path>,
    data_dir: Option<&std::path::Path>,
) -> Result<
    (
        backtest::backtest::engine_core::BacktestOutcome,
        std::path::PathBuf,
    ),
    HostError,
> {
    let configuration = backtest::backtest::job::load_backtest_config(config_path)
        .map_err(|error| HostError::Invalid(error.to_string()))?;
    let instances = build_strategy_instances(&configuration)?;
    backtest::backtest::job::run_native_job_with_data_dir(
        &configuration,
        &backtest::backtest::engine_core::EngineOptions::default(),
        output_dir,
        instances,
        data_dir,
    )
    .map_err(|error| HostError::Invalid(error.to_string()))
}
