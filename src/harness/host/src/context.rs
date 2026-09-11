//! Native Rust strategy context.
//!
//! [`PyEngineContext`] is the constrained strategy surface the native Rust
//! engine hands to a user-authored Python strategy through PyO3. It is the
//! native replacement for the Python `harness.strategy.context.StrategyContext`
//! bridge: the host owns the context itself, so the plugin surface no longer
//! imports the Python engine.
//!
//! The object is duck-typed to the historical Python context: it exposes
//! `clock`, `universe`, `books`, `orders`, `state`, `random`, `log`, `emissions`,
//! and `emit` with the same attribute and method names strategies already use.
//! `PyClock`, `PyOrders`, `PyBooks`, `PyStateStore`, `PyEmissions`, and `PyLog`
//! are the native capabilities. The per-context seeded generator is a standard
//! `random.Random(seed)` instance, so a strategy that draws from
//! `context.random` keeps the Python generator's exact sequence.

use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, BTreeSet};

use backtest::backtest::engine_core::EngineOrder;
use num::PyDecimal;
use pyo3::exceptions::{PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

/// Build a Python `decimal.Decimal` from one native decimal.
pub(crate) fn py_decimal<'py>(py: Python<'py>, value: PyDecimal) -> PyResult<Bound<'py, PyAny>> {
    py.import("decimal")?
        .getattr("Decimal")?
        .call1((value.to_string(),))
}

/// The canonical universe handed to a strategy.
#[pyclass(unsendable)]
pub struct PyUniverse {
    #[pyo3(get)]
    symbols: Vec<String>,
}

#[pymethods]
impl PyUniverse {
    /// Whether a symbol belongs to the universe.
    fn contains(&self, symbol: &str) -> bool {
        self.symbols.iter().any(|candidate| candidate == symbol)
    }

    fn __contains__(&self, symbol: &str) -> bool {
        self.contains(symbol)
    }

    fn __len__(&self) -> usize {
        self.symbols.len()
    }
}

/// A read-only clock that returns a Python `datetime` for the engine head.
#[pyclass(unsendable)]
pub struct PyClock {
    now_ns: Cell<i64>,
}

impl PyClock {
    /// Construct a clock pinned at one epoch-nanosecond instant.
    #[must_use]
    pub fn new(now_ns: i64) -> Self {
        Self {
            now_ns: Cell::new(now_ns),
        }
    }

    /// Advance the engine head to one epoch-nanosecond instant.
    pub fn set(&self, now_ns: i64) {
        self.now_ns.set(now_ns);
    }
}

#[pymethods]
impl PyClock {
    /// The engine head as a timezone-aware Python `datetime`.
    fn now(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let iso = backtest::backtest::manifest::utc_timestamp(self.now_ns.get(), false);
        let value = py
            .import("datetime")?
            .getattr("datetime")?
            .call_method1("fromisoformat", (iso,))?;
        Ok(value.unbind())
    }
}

/// A current position snapshot for one symbol.
#[pyclass(unsendable)]
pub struct PyPosition {
    #[pyo3(get)]
    symbol: String,
    quantity: PyDecimal,
}

#[pymethods]
impl PyPosition {
    /// The signed position quantity as a Python `decimal.Decimal`.
    #[getter]
    fn quantity(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(py_decimal(py, self.quantity)?.unbind())
    }
}

/// The strategy order sink: records submitted orders and their ownership.
#[pyclass(unsendable)]
pub struct PyOrders {
    pending: RefCell<Vec<EngineOrder>>,
    owned: RefCell<BTreeSet<String>>,
}

impl PyOrders {
    /// Construct an empty order sink.
    #[must_use]
    pub fn new() -> Self {
        Self {
            pending: RefCell::new(Vec::new()),
            owned: RefCell::new(BTreeSet::new()),
        }
    }

    /// Drop every order recorded since the last drain.
    pub fn clear_pending(&self) {
        self.pending.borrow_mut().clear();
    }

    /// Take the orders recorded since the last drain.
    pub fn take_pending(&self) -> Vec<EngineOrder> {
        std::mem::take(&mut *self.pending.borrow_mut())
    }
}

impl Default for PyOrders {
    fn default() -> Self {
        Self::new()
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

    /// Request cancellation of one order by identifier.
    fn cancel(&self, order_id: &str) {
        let _ = order_id;
    }

    /// Replace one order with a replacement intent recorded as a new submission.
    fn replace(&self, order_id: &str, replacement: &Bound<'_, PyAny>) -> PyResult<()> {
        let _ = order_id;
        let intent = order_from_python(replacement)?;
        self.owned.borrow_mut().insert(intent.order_id.clone());
        self.pending.borrow_mut().push(intent);
        Ok(())
    }

    /// Whether an order identifier was submitted by this strategy.
    fn owns(&self, order_id: &str) -> bool {
        self.owned.borrow().contains(order_id)
    }
}

/// Native read-only strategy book and account view.
///
/// The adapter applies every fill and mark, so a strategy observes the same
/// cash, positions, and marked equity the Rust engine accounts for.
#[pyclass(unsendable)]
pub struct PyBooks {
    book_id: String,
    initial_cash: PyDecimal,
    cash: RefCell<PyDecimal>,
    positions: RefCell<BTreeMap<String, PyDecimal>>,
    marks: RefCell<BTreeMap<String, PyDecimal>>,
    fill_count: Cell<u64>,
}

impl PyBooks {
    /// Construct a book view with one opening cash allocation.
    #[must_use]
    pub fn new(book_id: String, initial_cash: PyDecimal) -> Self {
        Self {
            book_id,
            initial_cash,
            cash: RefCell::new(initial_cash),
            positions: RefCell::new(BTreeMap::new()),
            marks: RefCell::new(BTreeMap::new()),
            fill_count: Cell::new(0),
        }
    }

    /// Apply one booked fill to cash, the signed position, and the fill count.
    pub fn apply_fill(
        &self,
        side: &str,
        symbol: &str,
        quantity: PyDecimal,
        price: PyDecimal,
        fee: PyDecimal,
    ) {
        let notional = quantity.checked_mul(price).unwrap_or(PyDecimal::ZERO);
        let mut cash = self.cash.borrow_mut();
        let updated = if side == "buy" {
            cash.checked_sub(notional)
                .and_then(|value| value.checked_sub(fee))
        } else {
            cash.checked_add(notional)
                .and_then(|value| value.checked_sub(fee))
        };
        if let Ok(value) = updated {
            *cash = value;
        }
        let signed = if side == "buy" {
            quantity
        } else {
            quantity.negated()
        };
        let mut positions = self.positions.borrow_mut();
        let entry = positions
            .entry(symbol.to_string())
            .or_insert(PyDecimal::ZERO);
        if let Ok(value) = entry.checked_add(signed) {
            *entry = value;
        }
        self.fill_count.set(self.fill_count.get() + 1);
    }

    /// Mark one symbol at the latest observed price.
    pub fn mark(&self, symbol: &str, price: PyDecimal) {
        self.marks.borrow_mut().insert(symbol.to_string(), price);
    }

    /// A copy of the current book state.
    fn snapshot(&self) -> Self {
        Self {
            book_id: self.book_id.clone(),
            initial_cash: self.initial_cash,
            cash: RefCell::new(*self.cash.borrow()),
            positions: RefCell::new(self.positions.borrow().clone()),
            marks: RefCell::new(self.marks.borrow().clone()),
            fill_count: Cell::new(self.fill_count.get()),
        }
    }
}

#[pymethods]
impl PyBooks {
    /// The strategy's virtual book identifier.
    #[getter]
    fn book_id(&self) -> &str {
        &self.book_id
    }

    /// The strategy's own virtual book identifiers.
    #[getter]
    fn book_ids(&self) -> (String,) {
        (self.book_id.clone(),)
    }

    /// Current strategy cash as a Python `decimal.Decimal`.
    #[getter]
    fn cash(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(py_decimal(py, *self.cash.borrow())?.unbind())
    }

    /// Marked strategy equity, or `None` when no position is marked.
    #[getter]
    fn equity(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let marks = self.marks.borrow();
        if marks.is_empty() {
            return Ok(None);
        }
        let mut total = *self.cash.borrow();
        for (symbol, quantity) in self.positions.borrow().iter() {
            if let Some(price) = marks.get(symbol) {
                let value = quantity.checked_mul(*price).unwrap_or(PyDecimal::ZERO);
                total = total.checked_add(value).unwrap_or(total);
            }
        }
        Ok(Some(py_decimal(py, total)?.unbind()))
    }

    /// The number of fills accounted for this strategy book.
    #[getter]
    fn fill_count(&self) -> u64 {
        self.fill_count.get()
    }

    /// The opening cash allocation.
    #[getter]
    fn initial_cash(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(py_decimal(py, self.initial_cash)?.unbind())
    }

    /// The current immutable book snapshot (this same view).
    #[getter]
    fn current(&self, py: Python<'_>) -> PyResult<Py<PyBooks>> {
        Py::new(py, self.snapshot())
    }

    /// One current position from this strategy book.
    fn position(&self, symbol: &str) -> PyPosition {
        let quantity = self
            .positions
            .borrow()
            .get(symbol)
            .copied()
            .unwrap_or(PyDecimal::ZERO);
        PyPosition {
            symbol: symbol.to_string(),
            quantity,
        }
    }

    /// Return the strategy's own book snapshot, rejecting sibling book ids.
    fn book(&self, py: Python<'_>, book_id: &str) -> PyResult<Py<PyBooks>> {
        if book_id == self.book_id {
            Py::new(py, self.snapshot())
        } else {
            Err(PyKeyError::new_err(format!("unknown book {book_id:?}")))
        }
    }
}

/// Deterministic strategy-local mutable state.
#[pyclass(unsendable)]
pub struct PyStateStore {
    values: RefCell<BTreeMap<String, Py<PyAny>>>,
}

impl PyStateStore {
    /// Construct an empty state store.
    #[must_use]
    pub fn new() -> Self {
        Self {
            values: RefCell::new(BTreeMap::new()),
        }
    }
}

impl Default for PyStateStore {
    fn default() -> Self {
        Self::new()
    }
}

#[pymethods]
impl PyStateStore {
    fn __setitem__(&self, key: String, value: Py<PyAny>) {
        self.values.borrow_mut().insert(key, value);
    }

    fn __getitem__(&self, py: Python<'_>, key: &str) -> PyResult<Py<PyAny>> {
        self.values
            .borrow()
            .get(key)
            .map(|value| value.clone_ref(py))
            .ok_or_else(|| PyKeyError::new_err(key.to_string()))
    }

    fn __delitem__(&self, key: &str) -> PyResult<()> {
        if self.values.borrow_mut().remove(key).is_some() {
            Ok(())
        } else {
            Err(PyKeyError::new_err(key.to_string()))
        }
    }

    fn __contains__(&self, key: &str) -> bool {
        self.values.borrow().contains_key(key)
    }

    fn __len__(&self) -> usize {
        self.values.borrow().len()
    }

    fn __iter__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let keys: Vec<String> = self.values.borrow().keys().cloned().collect();
        Ok(PyList::new(py, keys)?.unbind().into_any())
    }

    /// One stored value, or the supplied default when the key is absent.
    #[pyo3(signature = (key, default=None))]
    fn get(&self, py: Python<'_>, key: &str, default: Option<Py<PyAny>>) -> Option<Py<PyAny>> {
        self.values
            .borrow()
            .get(key)
            .map(|value| value.clone_ref(py))
            .or(default)
    }

    /// A shallow copy of the current state as a Python `dict`.
    fn snapshot(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let dict = PyDict::new(py);
        for (key, value) in self.values.borrow().iter() {
            dict.set_item(key, value.clone_ref(py))?;
        }
        Ok(dict.unbind())
    }
}

/// Deterministic structured emission sink.
#[pyclass(unsendable)]
pub struct PyEmissions {
    records: RefCell<Vec<(String, BTreeMap<String, serde_json::Value>)>>,
}

impl PyEmissions {
    /// Construct an empty emission sink.
    #[must_use]
    pub fn new() -> Self {
        Self {
            records: RefCell::new(Vec::new()),
        }
    }

    fn record(
        &self,
        name: &str,
        fields: Option<&Bound<'_, PyDict>>,
        values: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        if name.trim().is_empty() {
            return Err(PyTypeError::new_err(
                "emission name must be a non-empty string",
            ));
        }
        let mut combined = BTreeMap::new();
        for mapping in [fields, values].into_iter().flatten() {
            for (key, value) in mapping.iter() {
                combined.insert(key.extract::<String>()?, json_value(&value)?);
            }
        }
        self.records.borrow_mut().push((name.to_string(), combined));
        Ok(())
    }
}

impl Default for PyEmissions {
    fn default() -> Self {
        Self::new()
    }
}

#[pymethods]
impl PyEmissions {
    /// Record one named emission with structured fields.
    #[pyo3(signature = (name, fields=None, **values))]
    fn emit(
        &self,
        name: &str,
        fields: Option<&Bound<'_, PyDict>>,
        values: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        self.record(name, fields, values)
    }

    /// Every recorded emission in insertion order.
    #[getter]
    fn records(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let list = PyList::empty(py);
        for (name, fields) in self.records.borrow().iter() {
            let item = PyDict::new(py);
            item.set_item("name", name)?;
            item.set_item("fields", json_to_python(py, fields)?)?;
            list.append(item)?;
        }
        Ok(list.unbind())
    }
}

/// Deterministic structured log sink.
#[pyclass(unsendable)]
pub struct PyLog {
    records: RefCell<Vec<LogRecord>>,
}

/// One `(level, message, fields)` structured log record.
type LogRecord = (String, String, BTreeMap<String, serde_json::Value>);

impl PyLog {
    /// Construct an empty log sink.
    #[must_use]
    pub fn new() -> Self {
        Self {
            records: RefCell::new(Vec::new()),
        }
    }

    fn record(
        &self,
        level: &str,
        message: &str,
        fields: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        if message.trim().is_empty() {
            return Err(PyTypeError::new_err(
                "log message must be a non-empty string",
            ));
        }
        let mut collected = BTreeMap::new();
        if let Some(mapping) = fields {
            for (key, value) in mapping.iter() {
                collected.insert(key.extract::<String>()?, json_value(&value)?);
            }
        }
        self.records
            .borrow_mut()
            .push((level.to_string(), message.to_string(), collected));
        Ok(())
    }
}

impl Default for PyLog {
    fn default() -> Self {
        Self::new()
    }
}

#[pymethods]
impl PyLog {
    /// Record a debug message and its structured fields.
    #[pyo3(signature = (message, **fields))]
    fn debug(&self, message: &str, fields: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        self.record("debug", message, fields)
    }

    /// Record an informational message and its structured fields.
    #[pyo3(signature = (message, **fields))]
    fn info(&self, message: &str, fields: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        self.record("info", message, fields)
    }

    /// Record a warning message and its structured fields.
    #[pyo3(signature = (message, **fields))]
    fn warning(&self, message: &str, fields: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        self.record("warning", message, fields)
    }

    /// Record an error message and its structured fields.
    #[pyo3(signature = (message, **fields))]
    fn error(&self, message: &str, fields: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        self.record("error", message, fields)
    }

    /// Every recorded log record in insertion order.
    #[getter]
    fn records(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let list = PyList::empty(py);
        for (level, message, fields) in self.records.borrow().iter() {
            let item = PyDict::new(py);
            item.set_item("level", level)?;
            item.set_item("message", message)?;
            item.set_item("fields", json_to_python(py, fields)?)?;
            list.append(item)?;
        }
        Ok(list.unbind())
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
    #[pyo3(get)]
    books: Py<PyBooks>,
    #[pyo3(get)]
    state: Py<PyStateStore>,
    #[pyo3(get)]
    random: Py<PyAny>,
    #[pyo3(get)]
    log: Py<PyLog>,
    #[pyo3(get)]
    emissions: Py<PyEmissions>,
    #[pyo3(get)]
    book_id: String,
}

impl PyEngineContext {
    /// The native books capability, for the adapter's fill and mark updates.
    #[must_use]
    pub fn books_handle(&self) -> &Py<PyBooks> {
        &self.books
    }
}

#[pymethods]
impl PyEngineContext {
    /// The per-context seeded generator (alias for `random`).
    #[getter]
    fn rng(&self, py: Python<'_>) -> Py<PyAny> {
        self.random.clone_ref(py)
    }

    /// The structured logging sink (alias for `log`).
    #[getter]
    fn logger(&self, py: Python<'_>) -> Py<PyLog> {
        self.log.clone_ref(py)
    }

    /// Emit one structured signal through the injected sink.
    #[pyo3(signature = (name, fields=None, **values))]
    fn emit(
        &self,
        py: Python<'_>,
        name: &str,
        fields: Option<&Bound<'_, PyDict>>,
        values: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        self.emissions
            .bind(py)
            .borrow()
            .record(name, fields, values)
    }
}

/// One Python object rendered as a portable JSON value.
fn json_value(value: &Bound<'_, PyAny>) -> PyResult<serde_json::Value> {
    if value.is_none() {
        return Ok(serde_json::Value::Null);
    }
    if let Ok(boolean) = value.extract::<bool>() {
        return Ok(serde_json::Value::Bool(boolean));
    }
    if let Ok(integer) = value.extract::<i64>() {
        return Ok(serde_json::Value::from(integer));
    }
    if let Ok(text) = value.extract::<String>() {
        return Ok(serde_json::Value::String(text));
    }
    if let Ok(float) = value.extract::<f64>() {
        return Ok(serde_json::Number::from_f64(float)
            .map_or(serde_json::Value::Null, serde_json::Value::Number));
    }
    Ok(serde_json::Value::String(value.str()?.to_string()))
}

/// Convert a JSON object into a Python dict.
fn json_to_python<'py>(
    py: Python<'py>,
    value: &BTreeMap<String, serde_json::Value>,
) -> PyResult<Bound<'py, PyAny>> {
    let text =
        serde_json::to_string(value).map_err(|error| PyTypeError::new_err(error.to_string()))?;
    py.import("json")?.call_method1("loads", (text,))
}

/// Build one Python order intent from a strategy order object.
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

/// The native handles a started strategy runtime owns.
pub type BuiltContext = (Py<PyEngineContext>, Py<PyClock>, Py<PyOrders>, Py<PyBooks>);

/// Build the four native handles a started strategy runtime owns.
pub fn build_context(
    py: Python<'_>,
    universe: Vec<String>,
    start_ns: i64,
    book_id: String,
    initial_cash: PyDecimal,
    seed: u64,
) -> PyResult<BuiltContext> {
    let universe = Py::new(py, PyUniverse { symbols: universe })?;
    let clock = Py::new(py, PyClock::new(start_ns))?;
    let orders = Py::new(py, PyOrders::new())?;
    let books = Py::new(py, PyBooks::new(book_id.clone(), initial_cash))?;
    let state = Py::new(py, PyStateStore::new())?;
    let log = Py::new(py, PyLog::new())?;
    let emissions = Py::new(py, PyEmissions::new())?;
    let random = py
        .import("random")?
        .getattr("Random")?
        .call1((seed,))?
        .unbind();
    let context = Py::new(
        py,
        PyEngineContext {
            universe,
            clock: clock.clone_ref(py),
            orders: orders.clone_ref(py),
            books: books.clone_ref(py),
            state,
            random,
            log,
            emissions,
            book_id,
        },
    )?;
    Ok((context, clock, orders, books))
}

#[cfg(test)]
#[path = "../tests/unit/context.rs"]
mod tests;
