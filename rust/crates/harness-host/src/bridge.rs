//! PyO3-bridged strategy binding.
//!
//! The native [`crate::binding::StrategyBinding`] enforces the lifecycle on top
//! of a loaded strategy, but the Python `harness.strategy.binding.StrategyBinding`
//! remains the semantic authority for registering a context, refusing a second
//! start, and routing events. [`PyStrategyBinding`] owns the Python binding
//! object and drives it through PyO3, so the host observes the oracle's
//! decisions directly instead of a parallel implementation.

use std::collections::BTreeMap;
use std::path::PathBuf;

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::context::PyStrategyContext;
use crate::error::HostError;
use crate::python::{install_search_paths, json_dumps, json_loads};

/// A Python `StrategyBinding` driven through PyO3.
pub struct PyStrategyBinding {
    object: Py<PyAny>,
    search_paths: Vec<PathBuf>,
}

impl PyStrategyBinding {
    /// Instantiate a strategy class and bind it to the context via Python's
    /// `bind_strategy`.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the module or class is missing, the
    /// parameters are rejected, or the binding constructor raises.
    pub fn from_class(
        module: &str,
        class: &str,
        parameters: &BTreeMap<String, serde_json::Value>,
        context: &PyStrategyContext,
    ) -> Result<Self, HostError> {
        let search_paths = context.search_paths().to_vec();
        let result: PyResult<Py<PyAny>> = Python::attach(|py| {
            install_search_paths(py, &search_paths)?;
            let strategy_class = py.import(module)?.getattr(class)?;
            let parameter_object = PyDict::new(py);
            for (name, value) in parameters {
                parameter_object.set_item(name, json_loads(py, &value.to_string())?)?;
            }
            let binding = py
                .import("harness.strategy")?
                .getattr("bind_strategy")?
                .call1((strategy_class, parameter_object, context.python_object(py)))?;
            Ok(binding.unbind())
        });
        result
            .map(|object| Self {
                object,
                search_paths,
            })
            .map_err(HostError::from)
    }

    /// Register the binding once without starting callbacks.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the binding cannot subscribe.
    pub fn attach(&self) -> Result<(), HostError> {
        let result: PyResult<()> = Python::attach(|py| {
            self.object.bind(py).call_method0("attach")?;
            Ok(())
        });
        result.map_err(HostError::from)
    }

    /// Attach and invoke the strategy's start callback.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the binding is already started or the
    /// strategy's `on_start` raises.
    pub fn start(&self) -> Result<(), HostError> {
        let result: PyResult<()> = Python::attach(|py| {
            self.object.bind(py).call_method0("start")?;
            Ok(())
        });
        result.map_err(HostError::from)
    }

    /// Invoke the strategy's stop callback once.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the strategy's `on_stop` raises.
    pub fn stop(&self) -> Result<(), HostError> {
        let result: PyResult<()> = Python::attach(|py| {
            self.object.bind(py).call_method0("stop")?;
            Ok(())
        });
        result.map_err(HostError::from)
    }

    /// Whether the strategy has received `on_start`.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the attribute lookup fails.
    pub fn started(&self) -> Result<bool, HostError> {
        let result: PyResult<bool> =
            Python::attach(|py| self.object.bind(py).getattr("started")?.extract());
        result.map_err(HostError::from)
    }

    /// The Python context bound to this strategy.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the binding exposes no context.
    pub fn context(&self) -> Result<PyStrategyContext, HostError> {
        let search_paths = self.search_paths.clone();
        let result: PyResult<Py<PyAny>> =
            Python::attach(|py| Ok(self.object.bind(py).getattr("context")?.unbind()));
        result
            .map(|object| PyStrategyContext::from_object(object, search_paths))
            .map_err(HostError::from)
    }

    /// Invoke one strategy method and encode its result as JSON.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the payload is invalid, the callback raises,
    /// or its result cannot be encoded.
    pub fn strategy_call_json(
        &self,
        callback: &str,
        payload_json: Option<&str>,
    ) -> Result<String, HostError> {
        let result: PyResult<String> = Python::attach(|py| {
            let strategy = self.object.bind(py).getattr("strategy")?;
            let value = match payload_json {
                Some(text) => strategy.call_method1(callback, (json_loads(py, text)?,))?,
                None => strategy.call_method0(callback)?,
            };
            json_dumps(py, &value)
        });
        result.map_err(HostError::from)
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use pyo3::exceptions::PyRuntimeError;
    use pyo3::types::PyList;

    use super::*;
    use crate::context::{ContextSpec, StrategyContextBridge};
    use crate::python::{default_search_paths, install_harness_data_shim, install_search_paths};

    static COUNTER: AtomicUsize = AtomicUsize::new(0);

    const RECORDER_SOURCE: &str = r#"
from harness.strategy import Strategy


class Recorder(Strategy):
    def __init__(self, label):
        self.label = label
        self.active = False
        self.records = []

    def on_start(self, context):
        self.active = True
        self.records.append(
            {
                "phase": "start",
                "label": self.label,
                "book": context.book_id,
                "universe": [str(symbol) for symbol in context.universe.symbols],
                "draw": context.random.random(),
            }
        )

    def on_stop(self, context):
        self.active = False
        self.records.append({"phase": "stop", "book": context.book_id})

    def summary(self):
        return {"active": self.active, "records": self.records}
"#;

    fn temp_module() -> (PathBuf, String) {
        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!(
            "harness-host-bridge-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).expect("create temp dir");
        let module = format!("recorder_{}_{unique}", std::process::id());
        fs::write(dir.join(format!("{module}.py")), RECORDER_SOURCE).expect("write module");
        (dir, module)
    }

    fn spec() -> ContextSpec {
        ContextSpec::new("2024-01-19T14:30:00+00:00", "strategy")
            .with_universe(["spy", "aapl"])
            .with_seed(1729)
    }

    fn parameters() -> BTreeMap<String, serde_json::Value> {
        BTreeMap::from([("label".to_string(), serde_json::json!("alpha"))])
    }

    /// Build the reference context directly in Python, not through the bridge.
    fn oracle_context<'py>(py: Python<'py>, spec: &ContextSpec) -> Bound<'py, PyAny> {
        install_search_paths(py, &default_search_paths()).expect("paths");
        install_harness_data_shim(py).expect("shim");
        let stamp = py
            .import("datetime")
            .expect("datetime")
            .getattr("datetime")
            .expect("datetime")
            .call_method1("fromisoformat", (&spec.clock,))
            .expect("fromisoformat");
        let clock = py
            .import("harness.core.clock")
            .expect("clock")
            .getattr("SimClock")
            .expect("SimClock")
            .call1((stamp,))
            .expect("clock");
        let bus = py
            .import("harness.core.events")
            .expect("events")
            .getattr("EventBus")
            .expect("EventBus")
            .call1((&clock,))
            .expect("bus");
        let hub = py
            .import("harness.portfolio")
            .expect("portfolio")
            .getattr("PortfolioHub")
            .expect("PortfolioHub")
            .call1((&spec.account_id,))
            .expect("hub");
        hub.call_method1("create_book", (&spec.book_id, &spec.initial_cash))
            .expect("book");
        let kwargs = PyDict::new(py);
        kwargs.set_item("portfolio", hub).expect("portfolio");
        kwargs.set_item("bus", bus).expect("bus");
        kwargs.set_item("book_id", &spec.book_id).expect("book");
        kwargs.set_item("seed", spec.seed).expect("seed");
        kwargs
            .set_item(
                "universe",
                PyList::new(py, spec.universe.iter().map(String::as_str)).expect("universe"),
            )
            .expect("universe");
        py.import("harness.strategy")
            .expect("strategy")
            .getattr("StrategyContext")
            .expect("StrategyContext")
            .call((&clock,), Some(&kwargs))
            .expect("context")
    }

    /// Drive the Python binding directly and return its summary and final
    /// started flag.
    fn oracle_summary(
        py: Python<'_>,
        spec: &ContextSpec,
        module: &str,
    ) -> (serde_json::Value, bool) {
        let context = oracle_context(py, spec);
        let class = py
            .import(module)
            .expect("module")
            .getattr("Recorder")
            .expect("class");
        let parameters = PyDict::new(py);
        parameters.set_item("label", "alpha").expect("label");
        let binding = py
            .import("harness.strategy")
            .expect("strategy")
            .getattr("bind_strategy")
            .expect("bind_strategy")
            .call1((class, parameters, context))
            .expect("binding");
        binding.call_method0("attach").expect("attach");
        binding.call_method0("attach").expect("attach idempotent");
        binding.call_method0("start").expect("start");
        let duplicate = binding.call_method0("start").expect_err("double start");
        assert!(duplicate.is_instance_of::<PyRuntimeError>(py));
        binding.call_method0("stop").expect("stop");
        let strategy = binding.getattr("strategy").expect("strategy");
        let summary = strategy.call_method0("summary").expect("summary");
        let active: bool = binding
            .getattr("started")
            .expect("started")
            .extract()
            .expect("bool");
        (
            serde_json::from_str(&json_dumps(py, &summary).expect("dumps")).expect("parse"),
            active,
        )
    }

    #[test]
    fn bridged_binding_matches_python_binding() {
        let (dir, module) = temp_module();
        let mut paths = default_search_paths();
        paths.push(dir.clone());
        let spec = spec();

        let bridge_context =
            PyStrategyContext::with_search_paths(paths.clone(), &spec).expect("bridge context");
        let binding =
            PyStrategyBinding::from_class(&module, "Recorder", &parameters(), &bridge_context)
                .expect("bridge binding");
        assert!(!binding.started().expect("started"));
        binding.attach().expect("attach");
        binding.attach().expect("attach is idempotent");
        assert!(!binding.started().expect("started"));
        binding.start().expect("start");
        assert!(binding.started().expect("started"));
        let duplicate = binding.start().expect_err("double start");
        assert!(matches!(duplicate, HostError::Python(_)), "{duplicate}");
        binding.stop().expect("stop");
        assert!(!binding.started().expect("started"));
        assert_eq!(
            binding.context().expect("context").book_id().expect("book"),
            "strategy"
        );
        let bridge_summary: serde_json::Value = serde_json::from_str(
            &binding
                .strategy_call_json("summary", None)
                .expect("summary"),
        )
        .expect("parse");

        let (oracle, oracle_active) = Python::attach(|py| oracle_summary(py, &spec, &module));

        assert_eq!(bridge_summary, oracle);
        assert!(!oracle_active);
        let _ = fs::remove_dir_all(dir);
    }
}
