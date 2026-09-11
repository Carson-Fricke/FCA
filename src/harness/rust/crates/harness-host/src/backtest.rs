//! Native-engine plugin adapter for user-authored Python strategies.
//!
//! [`PyStrategyRuntime`] implements
//! [`harness_backtest::backtest::engine_core::StrategyRuntime`] over an
//! unmodified Python strategy class. The Rust engine owns the clock, event bus,
//! venue, fees, portfolio, and risk; the adapter hands the strategy a
//! constrained context object (clock, universe, order sink, emissions) and
//! converts the Python orders it submits back into engine intents.

use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use harness_backtest::backtest::engine_core::{
    EngineFill, EngineMarket, EngineOrder, StrategyRuntime, StrategyStart,
};
use harness_backtest::backtest::errors::BacktestError;
use harness_num::PyDecimal;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::error::HostError;
use crate::python::{default_search_paths, install_harness_data_shim, install_search_paths};

/// The canonical universe handed to a strategy.
#[pyclass(unsendable)]
pub struct PyUniverse {
    #[pyo3(get)]
    symbols: Vec<String>,
}

/// A read-only clock that returns a Python `datetime` for the engine head.
#[pyclass(unsendable)]
pub struct PyClock {
    now_ns: Cell<i64>,
}

#[pymethods]
impl PyClock {
    /// The engine head as a timezone-aware Python `datetime`.
    fn now(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let iso = harness_backtest::backtest::manifest::utc_timestamp(self.now_ns.get(), false);
        let value = py
            .import("datetime")?
            .getattr("datetime")?
            .call_method1("fromisoformat", (iso,))?;
        Ok(value.unbind())
    }
}

/// The strategy order sink: records submitted orders and ownership.
#[pyclass(unsendable)]
pub struct PyOrders {
    pending: RefCell<Vec<EngineOrder>>,
    owned: RefCell<BTreeSet<String>>,
}

impl PyOrders {
    fn clear_pending(&self) {
        self.pending.borrow_mut().clear();
    }

    fn take_pending(&self) -> Vec<EngineOrder> {
        std::mem::take(&mut *self.pending.borrow_mut())
    }
}

#[pymethods]
impl PyOrders {
    /// Record one submitted order without synchronously producing an outcome.
    fn submit(&self, order: &Bound<'_, PyAny>) -> PyResult<()> {
        let intent = order_from_python(order)?;
        self.owned.borrow_mut().insert(intent.order_id.clone());
        self.pending.borrow_mut().push(intent);
        Ok(())
    }

    /// Whether an order identifier was submitted by this strategy.
    fn owns(&self, order_id: &str) -> bool {
        self.owned.borrow().contains(order_id)
    }
}

/// The constrained context handed to a Python strategy.
#[pyclass(unsendable)]
pub struct PyEngineContext {
    #[pyo3(get)]
    universe: Py<PyUniverse>,
    #[pyo3(get)]
    clock: Py<PyClock>,
    #[pyo3(get)]
    orders: Py<PyOrders>,
}

#[pymethods]
impl PyEngineContext {
    /// Accept one structured emission (recorded by the engine as a no-op sink).
    #[pyo3(signature = (name, **kwargs))]
    fn emit(&self, name: &str, kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        let _ = (name, kwargs);
        Ok(())
    }
}

/// The four PyO3 handles a started strategy runtime owns.
type BuiltRuntime = (Py<PyAny>, Py<PyEngineContext>, Py<PyClock>, Py<PyOrders>);

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
        })
    }

    /// The instance identifier is owned by the engine, not the adapter.
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

        let built: Result<BuiltRuntime, HostError> =
            Python::attach(|py| {
                install_search_paths(py, &search_paths)?;
                install_harness_data_shim(py)?;
                let universe = Py::new(
                    py,
                    PyUniverse {
                        symbols: symbols.clone(),
                    },
                )?;
                let clock = Py::new(py, PyClock { now_ns: Cell::new(now_ns) })?;
                let orders = Py::new(
                    py,
                    PyOrders {
                        pending: RefCell::new(Vec::new()),
                        owned: RefCell::new(BTreeSet::new()),
                    },
                )?;
                let context = Py::new(
                    py,
                    PyEngineContext {
                        universe,
                        clock: clock.clone_ref(py),
                        orders: orders.clone_ref(py),
                    },
                )?;
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
                        class_obj.call((), Some(&fallback)).map_err(|_| first_error)?
                    }
                };
                instance.call_method1("on_start", (context.bind(py),))?;
                Ok((instance.unbind(), context, clock, orders))
            });
        let (strategy, context, clock, orders) = built.map_err(host_to_backtest)?;
        self.strategy = Some(strategy);
        self.context = Some(context);
        self.clock = Some(clock);
        self.orders = Some(orders);
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
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("context not built".to_string()))?;
        let clock = self
            .clock
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("clock not built".to_string()))?;
        let orders = self
            .orders
            .as_ref()
            .ok_or_else(|| BacktestError::StrategyExecution("orders not built".to_string()))?;
        let timestamp_ns = event.timestamp_ns();
        let result: Result<Vec<EngineOrder>, HostError> = Python::attach(|py| {
            clock.bind(py).borrow().now_ns.set(timestamp_ns);
            orders.bind(py).borrow().clear_pending();
            let callback = match event {
                EngineMarket::Quote { .. } => "on_quote",
                EngineMarket::Trade { .. } => "on_trade",
            };
            let py_event = market_to_python(py, event)?;
            strategy
                .bind(py)
                .call_method1(callback, (py_event,))?;
            let _ = context;
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
        let result: Result<(), HostError> = Python::attach(|py| {
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
    let iso = harness_backtest::backtest::manifest::utc_timestamp(timestamp_ns, false);
    Ok(py
        .import("datetime")?
        .getattr("datetime")?
        .call_method1("fromisoformat", (iso,))?)
}

fn order_from_python(order: &Bound<'_, PyAny>) -> PyResult<EngineOrder> {
    let order_id: String = order.getattr("order_id")?.extract()?;
    let symbol: String = order.getattr("symbol")?.str()?.to_string();
    let side = enum_string(&order.getattr("side")?)?;
    let quantity = decimal_value(&order.getattr("quantity")?)?;
    let order_type = enum_string(&order.getattr("order_type")?)?;
    let time_in_force = enum_string(&order.getattr("time_in_force")?)?;
    let limit_price = optional_decimal(&order.getattr("limit_price")?)?;
    let stop_price = optional_decimal(&order.getattr("stop_price")?)?;
    let event_id: String = order.getattr("event_id")?.extract()?;
    let sequence: u64 = order.getattr("sequence")?.extract()?;
    Ok(EngineOrder {
        order_id,
        symbol,
        side,
        quantity,
        order_type,
        time_in_force,
        limit_price,
        stop_price,
        timestamp_ns: 0,
        sequence,
        event_id,
    })
}

fn enum_string(value: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(text) = value.extract::<String>() {
        return Ok(text);
    }
    if let Ok(member) = value.getattr("value") {
        if let Ok(text) = member.extract::<String>() {
            return Ok(text);
        }
    }
    Ok(value.str()?.to_string())
}

fn decimal_value(value: &Bound<'_, PyAny>) -> PyResult<PyDecimal> {
    let text = value.str()?.to_string();
    text.parse::<PyDecimal>()
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}

fn optional_decimal(value: &Bound<'_, PyAny>) -> PyResult<Option<PyDecimal>> {
    if value.is_none() {
        Ok(None)
    } else {
        Ok(Some(decimal_value(value)?))
    }
}

/// Extract the per-strategy parameters from a configuration JSON object.
#[must_use]
pub fn parameters_from_json(
    parameters: &BTreeMap<String, harness_backtest::canonical_json::JsonValue>,
) -> BTreeMap<String, serde_json::Value> {
    parameters
        .iter()
        .map(|(key, value)| (key.clone(), json_to_serde(value)))
        .collect()
}

fn json_to_serde(value: &harness_backtest::canonical_json::JsonValue) -> serde_json::Value {
    use harness_backtest::canonical_json::JsonValue;
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
    configuration: &harness_backtest::backtest::config::BacktestConfig,
) -> Result<
    Vec<
        harness_backtest::backtest::engine_core::StrategyInstance,
    >,
    HostError,
> {
    let mut instances = Vec::new();
    for configured in &configuration.strategy_instances {
        let runtime = PyStrategyRuntime::new(
            &configured.implementation,
            parameters_from_json(&configured.parameters),
        )?;
        instances.push(harness_backtest::backtest::engine_core::StrategyInstance {
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
        harness_backtest::backtest::engine_core::BacktestOutcome,
        std::path::PathBuf,
    ),
    HostError,
> {
    let configuration = harness_backtest::backtest::job::load_backtest_config(config_path)
        .map_err(|error| HostError::Invalid(error.to_string()))?;
    let instances = build_strategy_instances(&configuration)?;
    harness_backtest::backtest::job::run_native_job(
        &configuration,
        &harness_backtest::backtest::engine_core::EngineOptions::default(),
        output_dir,
        instances,
    )
    .map_err(|error| HostError::Invalid(error.to_string()))
}
