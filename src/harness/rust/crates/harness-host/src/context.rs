//! PyO3-bridged strategy context.
//!
//! The Rust engine needs the constrained strategy dependencies (clock, books,
//! state, seeded randomness, logging, emissions, and subscriptions) without a
//! second implementation of `harness.strategy.context.StrategyContext`. This
//! module exposes [`StrategyContextBridge`], the Rust-side surface the host
//! programs against, and [`PyStrategyContext`], a PyO3 implementation that owns
//! and drives the *Python* context object. Because every accessor executes
//! Python, the observed behavior is parity-by-construction with the oracle in
//! `harness.strategy`.
//!
//! The PyO3 implementation lives behind the default `pyo3-bridge` feature so a
//! consumer that only wants the native linter can opt out.

use std::collections::BTreeMap;
use std::path::PathBuf;

use pyo3::exceptions::{PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::error::HostError;
use crate::python::{
    default_search_paths, install_harness_data_shim, install_search_paths, json_dumps_value,
    json_loads,
};

/// Rust-side view of one strategy's constrained context.
///
/// The methods deliberately mirror the observable Python surface rather than
/// introducing Rust-only semantics. A host can depend on `dyn
/// StrategyContextBridge` and stay independent of the interpreter embedding.
pub trait StrategyContextBridge {
    /// The strategy's virtual book identifier.
    fn book_id(&self) -> Result<String, HostError>;

    /// The canonical, sorted strategy universe.
    fn universe(&self) -> Result<Vec<String>, HostError>;

    /// Whether a symbol belongs to the universe.
    fn contains_symbol(&self, symbol: &str) -> Result<bool, HostError>;

    /// The engine clock head in ISO-8601 form.
    fn clock_now(&self) -> Result<String, HostError>;

    /// Current strategy cash rendered as a decimal string.
    fn cash(&self) -> Result<String, HostError>;

    /// Marked strategy equity rendered as a decimal string, if marked.
    fn equity(&self) -> Result<Option<String>, HostError>;

    /// The number of fills accounted for this strategy book.
    fn fill_count(&self) -> Result<u64, HostError>;

    /// The frozen strategy state as a JSON object.
    fn state_snapshot_json(&self) -> Result<String, HostError>;

    /// One frozen strategy state value encoded as JSON, or `None` when absent.
    fn state_get_json(&self, key: &str) -> Result<Option<String>, HostError>;

    /// Store one strategy state value from a JSON document.
    fn state_set_json(&self, key: &str, value_json: &str) -> Result<(), HostError>;

    /// One draw from the per-context seeded generator.
    fn random_draw(&self) -> Result<f64, HostError>;

    /// One integer draw from the per-context seeded generator.
    fn random_int(&self, low: i64, high: i64) -> Result<i64, HostError>;

    /// Emit one structured signal from a JSON object of fields.
    fn emit_json(&self, name: &str, fields_json: &str) -> Result<(), HostError>;

    /// Every structured emission as a JSON array.
    fn emissions_json(&self) -> Result<String, HostError>;

    /// Append one structured log record.
    fn log_json(&self, level: &str, message: &str, fields_json: &str) -> Result<(), HostError>;

    /// Every structured log record as a JSON array.
    fn log_records_json(&self) -> Result<String, HostError>;
}

/// The description used to build a Python `StrategyContext`.
///
/// The spec is data, not behavior: construction delegates every semantic
/// decision (canonicalization, freezing, seed validation) to Python.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextSpec {
    /// ISO-8601 engine clock stamp.
    pub clock: String,
    /// Shared account identifier.
    pub account_id: String,
    /// The strategy's virtual book identifier.
    pub book_id: String,
    /// Decimal-rendered initial book cash.
    pub initial_cash: String,
    /// Raw universe members, canonicalized by Python.
    pub universe: Vec<String>,
    /// Seed for the per-context deterministic generator.
    pub seed: i64,
    /// Initial frozen strategy state.
    pub state: BTreeMap<String, serde_json::Value>,
}

impl ContextSpec {
    /// Construct a spec with the default account, cash, seed, and empty state.
    #[must_use]
    pub fn new(clock: impl Into<String>, book_id: impl Into<String>) -> Self {
        Self {
            clock: clock.into(),
            account_id: "account".to_string(),
            book_id: book_id.into(),
            initial_cash: "1000".to_string(),
            universe: Vec::new(),
            seed: 0,
            state: BTreeMap::new(),
        }
    }

    /// Override the shared account identifier.
    #[must_use]
    pub fn with_account_id(mut self, account_id: impl Into<String>) -> Self {
        self.account_id = account_id.into();
        self
    }

    /// Override the initial book cash.
    #[must_use]
    pub fn with_initial_cash(mut self, initial_cash: impl Into<String>) -> Self {
        self.initial_cash = initial_cash.into();
        self
    }

    /// Set the raw universe members.
    #[must_use]
    pub fn with_universe<I, S>(mut self, universe: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.universe = universe.into_iter().map(Into::into).collect();
        self
    }

    /// Set the deterministic generator seed.
    #[must_use]
    pub fn with_seed(mut self, seed: i64) -> Self {
        self.seed = seed;
        self
    }

    /// Add one initial strategy state entry.
    #[must_use]
    pub fn with_state_entry(mut self, key: impl Into<String>, value: serde_json::Value) -> Self {
        self.state.insert(key.into(), value);
        self
    }
}

/// A Python `StrategyContext` driven through PyO3.
pub struct PyStrategyContext {
    object: Py<PyAny>,
    search_paths: Vec<PathBuf>,
}

impl PyStrategyContext {
    /// Build and own a Python context using the checkout's default paths.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when Python is unavailable or the context
    /// constructor rejects the spec.
    pub fn new(spec: &ContextSpec) -> Result<Self, HostError> {
        Self::with_search_paths(default_search_paths(), spec)
    }

    /// Build and own a Python context with explicit import search paths.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when Python is unavailable or the context
    /// constructor rejects the spec.
    pub fn with_search_paths(
        search_paths: Vec<PathBuf>,
        spec: &ContextSpec,
    ) -> Result<Self, HostError> {
        let result: PyResult<Py<PyAny>> = Python::attach(|py| {
            install_search_paths(py, &search_paths)?;
            install_harness_data_shim(py)?;
            let stamp = py
                .import("datetime")?
                .getattr("datetime")?
                .call_method1("fromisoformat", (&spec.clock,))?;
            let clock = py
                .import("harness.core.clock")?
                .getattr("SimClock")?
                .call1((stamp,))?;
            let bus = py
                .import("harness.core.events")?
                .getattr("EventBus")?
                .call1((&clock,))?;
            let portfolio = py.import("harness.portfolio")?;
            let hub = portfolio
                .getattr("PortfolioHub")?
                .call1((&spec.account_id,))?;
            hub.call_method1("create_book", (&spec.book_id, &spec.initial_cash))?;
            let context_class = py.import("harness.strategy")?.getattr("StrategyContext")?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("portfolio", hub)?;
            kwargs.set_item("bus", bus)?;
            kwargs.set_item("book_id", &spec.book_id)?;
            kwargs.set_item("seed", spec.seed)?;
            if !spec.universe.is_empty() {
                kwargs.set_item(
                    "universe",
                    PyList::new(py, spec.universe.iter().map(String::as_str))?,
                )?;
            }
            if !spec.state.is_empty() {
                let state = PyDict::new(py);
                for (key, value) in &spec.state {
                    state.set_item(key, json_loads(py, &value.to_string())?)?;
                }
                kwargs.set_item("state", state)?;
            }
            Ok(context_class.call((&clock,), Some(&kwargs))?.unbind())
        });
        result
            .map(|object| Self {
                object,
                search_paths,
            })
            .map_err(HostError::from)
    }

    /// Borrow the wrapped Python context for a parity oracle or a Python-side
    /// call the bridge does not expose.
    pub fn python_object<'py>(&self, py: Python<'py>) -> Bound<'py, PyAny> {
        self.object.bind(py).clone()
    }

    /// The import search paths used to build this context.
    #[must_use]
    pub(crate) fn search_paths(&self) -> &[PathBuf] {
        &self.search_paths
    }

    /// Wrap an already-materialized Python context object.
    pub(crate) fn from_object(object: Py<PyAny>, search_paths: Vec<PathBuf>) -> Self {
        Self {
            object,
            search_paths,
        }
    }
}

impl StrategyContextBridge for PyStrategyContext {
    fn book_id(&self) -> Result<String, HostError> {
        let result: PyResult<String> =
            Python::attach(|py| self.object.bind(py).getattr("book_id")?.extract());
        result.map_err(HostError::from)
    }

    fn universe(&self) -> Result<Vec<String>, HostError> {
        let result: PyResult<Vec<String>> = Python::attach(|py| {
            let symbols = self
                .object
                .bind(py)
                .getattr("universe")?
                .getattr("symbols")?;
            let mut members = Vec::new();
            for symbol in symbols.try_iter()? {
                members.push(symbol?.str()?.to_string());
            }
            Ok(members)
        });
        result.map_err(HostError::from)
    }

    fn contains_symbol(&self, symbol: &str) -> Result<bool, HostError> {
        let result: PyResult<bool> = Python::attach(|py| {
            self.object
                .bind(py)
                .getattr("universe")?
                .call_method1("contains", (symbol,))?
                .extract()
        });
        result.map_err(HostError::from)
    }

    fn clock_now(&self) -> Result<String, HostError> {
        let result: PyResult<String> = Python::attach(|py| {
            self.object
                .bind(py)
                .getattr("clock")?
                .call_method0("now")?
                .call_method0("isoformat")?
                .extract()
        });
        result.map_err(HostError::from)
    }

    fn cash(&self) -> Result<String, HostError> {
        let result: PyResult<String> = Python::attach(|py| {
            Ok(self
                .object
                .bind(py)
                .getattr("books")?
                .getattr("cash")?
                .str()?
                .to_string())
        });
        result.map_err(HostError::from)
    }

    fn equity(&self) -> Result<Option<String>, HostError> {
        let result: PyResult<Option<String>> = Python::attach(|py| {
            let value = self.object.bind(py).getattr("books")?.getattr("equity")?;
            if value.is_none() {
                Ok(None)
            } else {
                Ok(Some(value.str()?.to_string()))
            }
        });
        result.map_err(HostError::from)
    }

    fn fill_count(&self) -> Result<u64, HostError> {
        let result: PyResult<u64> = Python::attach(|py| {
            self.object
                .bind(py)
                .getattr("books")?
                .getattr("fill_count")?
                .extract()
        });
        result.map_err(HostError::from)
    }

    fn state_snapshot_json(&self) -> Result<String, HostError> {
        let result: PyResult<String> = Python::attach(|py| {
            let snapshot = self.object.bind(py).getattr("state")?.getattr("snapshot")?;
            json_dumps_value(py, &snapshot)
        });
        result.map_err(HostError::from)
    }

    fn state_get_json(&self, key: &str) -> Result<Option<String>, HostError> {
        let result: PyResult<Option<String>> = Python::attach(|py| {
            let state = self.object.bind(py).getattr("state")?;
            match state.get_item(key) {
                Ok(value) => Ok(Some(json_dumps_value(py, &value)?)),
                Err(error) if error.is_instance_of::<PyKeyError>(py) => Ok(None),
                Err(error) => Err(error),
            }
        });
        result.map_err(HostError::from)
    }

    fn state_set_json(&self, key: &str, value_json: &str) -> Result<(), HostError> {
        let result: PyResult<()> = Python::attach(|py| {
            let value = json_loads(py, value_json)?;
            self.object.bind(py).getattr("state")?.set_item(key, value)
        });
        result.map_err(HostError::from)
    }

    fn random_draw(&self) -> Result<f64, HostError> {
        let result: PyResult<f64> = Python::attach(|py| {
            self.object
                .bind(py)
                .getattr("random")?
                .call_method0("random")?
                .extract()
        });
        result.map_err(HostError::from)
    }

    fn random_int(&self, low: i64, high: i64) -> Result<i64, HostError> {
        let result: PyResult<i64> = Python::attach(|py| {
            self.object
                .bind(py)
                .getattr("random")?
                .call_method1("randint", (low, high))?
                .extract()
        });
        result.map_err(HostError::from)
    }

    fn emit_json(&self, name: &str, fields_json: &str) -> Result<(), HostError> {
        let result: PyResult<()> = Python::attach(|py| {
            let fields = json_loads(py, fields_json)?;
            self.object.bind(py).call_method1("emit", (name, fields))?;
            Ok(())
        });
        result.map_err(HostError::from)
    }

    fn emissions_json(&self) -> Result<String, HostError> {
        let result: PyResult<String> = Python::attach(|py| {
            let records = self
                .object
                .bind(py)
                .getattr("emissions")?
                .getattr("records")?;
            let array = PyList::empty(py);
            for record in records.try_iter()? {
                let record = record?;
                let item = PyDict::new(py);
                item.set_item("name", record.getattr("name")?)?;
                item.set_item("fields", record.getattr("fields")?)?;
                array.append(item)?;
            }
            json_dumps_value(py, array.as_any())
        });
        result.map_err(HostError::from)
    }

    fn log_json(&self, level: &str, message: &str, fields_json: &str) -> Result<(), HostError> {
        let result: PyResult<()> = Python::attach(|py| {
            let fields = json_loads(py, fields_json)?;
            let kwargs = fields
                .cast::<PyDict>()
                .map_err(|_| PyTypeError::new_err("log fields must be a JSON object"))?;
            let method = self.object.bind(py).getattr("log")?.getattr(level)?;
            method.call((message,), Some(kwargs))?;
            Ok(())
        });
        result.map_err(HostError::from)
    }

    fn log_records_json(&self) -> Result<String, HostError> {
        let result: PyResult<String> = Python::attach(|py| {
            let records = self.object.bind(py).getattr("log")?.getattr("records")?;
            let array = PyList::empty(py);
            for record in records.try_iter()? {
                let record = record?;
                let item = PyDict::new(py);
                item.set_item("level", record.getattr("level")?)?;
                item.set_item("message", record.getattr("message")?)?;
                item.set_item("fields", record.getattr("fields")?)?;
                array.append(item)?;
            }
            json_dumps_value(py, array.as_any())
        });
        result.map_err(HostError::from)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec() -> ContextSpec {
        ContextSpec::new("2024-01-19T14:30:00+00:00", "strategy")
            .with_universe(["spy", "aapl", "AAPL"])
            .with_seed(1729)
            .with_state_entry("alpha", serde_json::json!(1))
            .with_state_entry("beta", serde_json::json!("two"))
    }

    /// Build the same context directly in Python, independently of the bridge.
    fn oracle_context<'py>(py: Python<'py>, spec: &ContextSpec) -> PyResult<Bound<'py, PyAny>> {
        install_search_paths(py, &default_search_paths())?;
        install_harness_data_shim(py)?;
        let stamp = py
            .import("datetime")?
            .getattr("datetime")?
            .call_method1("fromisoformat", (&spec.clock,))?;
        let clock = py
            .import("harness.core.clock")?
            .getattr("SimClock")?
            .call1((stamp,))?;
        let bus = py
            .import("harness.core.events")?
            .getattr("EventBus")?
            .call1((&clock,))?;
        let portfolio = py.import("harness.portfolio")?;
        let hub = portfolio
            .getattr("PortfolioHub")?
            .call1((&spec.account_id,))?;
        hub.call_method1("create_book", (&spec.book_id, &spec.initial_cash))?;
        let context_class = py.import("harness.strategy")?.getattr("StrategyContext")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("portfolio", hub)?;
        kwargs.set_item("bus", bus)?;
        kwargs.set_item("book_id", &spec.book_id)?;
        kwargs.set_item("seed", spec.seed)?;
        kwargs.set_item(
            "universe",
            PyList::new(py, spec.universe.iter().map(String::as_str))?,
        )?;
        let state = PyDict::new(py);
        for (key, value) in &spec.state {
            state.set_item(key, json_loads(py, &value.to_string())?)?;
        }
        kwargs.set_item("state", state)?;
        context_class.call((&clock,), Some(&kwargs))
    }

    fn oracle_universe(context: &Bound<'_, PyAny>) -> Vec<String> {
        context
            .getattr("universe")
            .expect("universe")
            .getattr("symbols")
            .expect("symbols")
            .try_iter()
            .expect("iter")
            .map(|symbol| symbol.expect("symbol").str().expect("str").to_string())
            .collect()
    }

    fn oracle_state_json(py: Python<'_>, context: &Bound<'_, PyAny>) -> serde_json::Value {
        let snapshot = context
            .getattr("state")
            .expect("state")
            .getattr("snapshot")
            .expect("snapshot");
        let dict = py
            .import("builtins")
            .expect("builtins")
            .getattr("dict")
            .expect("dict")
            .call1((snapshot,))
            .expect("dict()");
        let text: String = py
            .import("json")
            .expect("json")
            .call_method1("dumps", (dict,))
            .expect("dumps")
            .extract()
            .expect("extract");
        serde_json::from_str(&text).expect("parse")
    }

    #[test]
    fn bridged_context_matches_python_construction() {
        let spec = spec();
        let bridge = PyStrategyContext::new(&spec).expect("bridge");
        Python::attach(|py| {
            let oracle = oracle_context(py, &spec).expect("oracle");
            assert_eq!(
                bridge.book_id().expect("book_id"),
                oracle
                    .getattr("book_id")
                    .expect("book_id")
                    .extract::<String>()
                    .expect("str")
            );
            assert_eq!(
                bridge.universe().expect("universe"),
                oracle_universe(&oracle)
            );
            assert_eq!(
                bridge.contains_symbol("aapl").expect("contains"),
                oracle
                    .getattr("universe")
                    .expect("universe")
                    .call_method1("contains", ("aapl",))
                    .expect("contains")
                    .extract::<bool>()
                    .expect("bool")
            );
            let expected_now: String = oracle
                .getattr("clock")
                .expect("clock")
                .call_method0("now")
                .expect("now")
                .call_method0("isoformat")
                .expect("isoformat")
                .extract()
                .expect("str");
            assert_eq!(bridge.clock_now().expect("clock_now"), expected_now);
            assert_eq!(
                bridge.cash().expect("cash"),
                oracle
                    .getattr("books")
                    .expect("books")
                    .getattr("cash")
                    .expect("cash")
                    .str()
                    .expect("str")
                    .to_string()
            );
            assert_eq!(
                bridge.fill_count().expect("fill_count"),
                oracle
                    .getattr("books")
                    .expect("books")
                    .getattr("fill_count")
                    .expect("fill_count")
                    .extract::<u64>()
                    .expect("u64")
            );
            assert_eq!(
                serde_json::from_str::<serde_json::Value>(
                    &bridge.state_snapshot_json().expect("state")
                )
                .expect("parse"),
                oracle_state_json(py, &oracle)
            );
        });
    }

    #[test]
    fn bridged_random_matches_the_seeded_python_generator() {
        let spec = spec();
        let bridge = PyStrategyContext::new(&spec).expect("bridge");
        Python::attach(|py| {
            let oracle = oracle_context(py, &spec).expect("oracle");
            let oracle_random = oracle.getattr("random").expect("random");
            for _ in 0..3 {
                assert_eq!(
                    bridge.random_draw().expect("draw"),
                    oracle_random
                        .call_method0("random")
                        .expect("random")
                        .extract::<f64>()
                        .expect("f64")
                );
            }
            assert_eq!(
                bridge.random_int(1, 10).expect("randint"),
                oracle_random
                    .call_method1("randint", (1, 10))
                    .expect("randint")
                    .extract::<i64>()
                    .expect("i64")
            );
        });
    }

    #[test]
    fn bridged_state_freezes_and_rejects_like_python() {
        let spec = spec();
        let bridge = PyStrategyContext::new(&spec).expect("bridge");
        assert_eq!(
            bridge.state_get_json("beta").expect("get"),
            Some("\"two\"".to_string())
        );
        assert_eq!(bridge.state_get_json("missing").expect("get"), None);

        bridge
            .state_set_json("gamma", "{\"nested\": {\"b\": 2}}")
            .expect("set");
        assert_eq!(
            bridge.state_get_json("gamma").expect("get"),
            Some("{\"nested\":{\"b\":2}}".to_string())
        );

        let rejection = bridge.state_set_json("bad", "1.5").expect_err("float");
        assert!(matches!(rejection, HostError::Python(_)), "{rejection}");
    }

    #[test]
    fn bridged_emissions_and_logs_match_python() {
        let spec = spec();
        let bridge = PyStrategyContext::new(&spec).expect("bridge");
        bridge
            .emit_json("metric", "{\"v\": 1}")
            .expect("bridge emit");
        bridge
            .log_json("info", "hello", "{\"a\": 1}")
            .expect("bridge log");

        Python::attach(|py| {
            let oracle = oracle_context(py, &spec).expect("oracle");
            let fields = json_loads(py, "{\"v\": 1}").expect("fields");
            oracle
                .call_method1("emit", ("metric", fields))
                .expect("oracle emit");
            let kwargs = PyDict::new(py);
            kwargs.set_item("a", 1).expect("a");
            oracle
                .getattr("log")
                .expect("log")
                .getattr("info")
                .expect("info")
                .call(("hello",), Some(&kwargs))
                .expect("oracle log");

            let oracle_emissions: String = {
                let array = PyList::empty(py);
                for record in oracle
                    .getattr("emissions")
                    .expect("emissions")
                    .getattr("records")
                    .expect("records")
                    .try_iter()
                    .expect("iter")
                {
                    let record = record.expect("record");
                    let item = PyDict::new(py);
                    item.set_item("name", record.getattr("name").expect("name"))
                        .expect("name");
                    item.set_item("fields", record.getattr("fields").expect("fields"))
                        .expect("fields");
                    array.append(item).expect("append");
                }
                json_dumps_value(py, array.as_any()).expect("emissions json")
            };
            let oracle_logs: String = {
                let array = PyList::empty(py);
                for record in oracle
                    .getattr("log")
                    .expect("log")
                    .getattr("records")
                    .expect("records")
                    .try_iter()
                    .expect("iter")
                {
                    let record = record.expect("record");
                    let item = PyDict::new(py);
                    item.set_item("level", record.getattr("level").expect("level"))
                        .expect("level");
                    item.set_item("message", record.getattr("message").expect("message"))
                        .expect("message");
                    item.set_item("fields", record.getattr("fields").expect("fields"))
                        .expect("fields");
                    array.append(item).expect("append");
                }
                json_dumps_value(py, array.as_any()).expect("logs json")
            };

            assert_eq!(
                serde_json::from_str::<serde_json::Value>(
                    &bridge.emissions_json().expect("bridge emissions")
                )
                .expect("parse"),
                serde_json::from_str::<serde_json::Value>(&oracle_emissions).expect("parse")
            );
            assert_eq!(
                serde_json::from_str::<serde_json::Value>(
                    &bridge.log_records_json().expect("bridge logs")
                )
                .expect("parse"),
                serde_json::from_str::<serde_json::Value>(&oracle_logs).expect("parse")
            );
        });
    }

    #[test]
    fn host_can_program_against_the_trait() {
        fn summarize(context: &dyn StrategyContextBridge) -> Result<String, HostError> {
            let universe = context.universe()?;
            Ok(format!("{}:{}", context.book_id()?, universe.join(",")))
        }

        let bridge = PyStrategyContext::new(&spec()).expect("bridge");
        assert_eq!(summarize(&bridge).expect("summary"), "strategy:AAPL,SPY");
    }
}
