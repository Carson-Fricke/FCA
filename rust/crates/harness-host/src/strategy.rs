//! Host for user-authored strategy classes.
//!
//! A strategy is loaded from an importable Python module, instantiated with
//! validated parameters, and driven by lifecycle callbacks. Loading and
//! dispatch never require the engine's synchronous context types: events
//! cross the boundary as JSON payloads, so user strategies from
//! `strategies/` run unmodified against the Rust runtime's event stream.

use std::collections::BTreeMap;
use std::path::PathBuf;

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::error::HostError;
use crate::python::{install_search_paths, json_dumps, json_loads};

/// Minimum lifecycle callbacks a loaded strategy must expose.
const REQUIRED_CALLBACKS: &[&str] = &["on_start", "on_stop"];

/// One strategy to load: module path, class name, and constructor parameters.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrategySpec {
    /// Importable module path (for example `strategies.harness_examples.intraday_momentum`).
    pub module: String,
    /// Class name inside the module.
    pub class: String,
    /// Constructor keyword parameters.
    pub parameters: BTreeMap<String, serde_json::Value>,
}

impl StrategySpec {
    /// Construct a spec with no parameters.
    #[must_use]
    pub fn new(module: impl Into<String>, class: impl Into<String>) -> Self {
        Self {
            module: module.into(),
            class: class.into(),
            parameters: BTreeMap::new(),
        }
    }

    /// Set one constructor parameter.
    #[must_use]
    pub fn with_parameter(mut self, name: impl Into<String>, value: serde_json::Value) -> Self {
        self.parameters.insert(name.into(), value);
        self
    }
}

/// One callback dispatch: a callback name and its JSON event payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrategyEvent {
    /// Callback method name (for example `on_quote`).
    pub callback: String,
    /// JSON payload passed as the callback's single argument.
    pub payload_json: String,
}

impl StrategyEvent {
    /// Construct one callback dispatch.
    #[must_use]
    pub fn new(callback: impl Into<String>, payload_json: impl Into<String>) -> Self {
        Self {
            callback: callback.into(),
            payload_json: payload_json.into(),
        }
    }
}

/// Host that imports user strategy modules.
#[derive(Debug, Clone, Default)]
pub struct StrategyHost {
    search_paths: Vec<PathBuf>,
}

impl StrategyHost {
    /// Construct a host with no extra search paths.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Construct a host that prepends `paths` to `sys.path`.
    #[must_use]
    pub fn with_search_paths(paths: Vec<PathBuf>) -> Self {
        Self {
            search_paths: paths,
        }
    }

    /// Add one directory to the module search path.
    pub fn add_search_path(&mut self, path: impl Into<PathBuf>) {
        self.search_paths.push(path.into());
    }

    /// Import, instantiate, and validate one strategy.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the module or class is missing, the
    /// constructor rejects the parameters, or a required callback is absent.
    pub fn load(&self, spec: &StrategySpec) -> Result<LoadedStrategy, HostError> {
        let search_paths = self.search_paths.clone();
        let result: PyResult<LoadedStrategy> = Python::attach(|py| {
            install_search_paths(py, &search_paths)?;
            let module = py.import(spec.module.as_str())?;
            let class = module.getattr(spec.class.as_str()).map_err(|error| {
                pyo3::exceptions::PyAttributeError::new_err(format!(
                    "{}: {error}",
                    spec.class
                ))
            })?;
            let kwargs = PyDict::new(py);
            for (name, value) in &spec.parameters {
                kwargs.set_item(name, json_loads(py, &value.to_string())?)?;
            }
            let instance = match class.call((), Some(&kwargs)) {
                Ok(instance) => instance,
                Err(first_error) => {
                    let fallback = PyDict::new(py);
                    fallback.set_item("parameters", &kwargs)?;
                    class
                        .call((), Some(&fallback))
                        .map_err(|_| first_error)?
                }
            };
            for callback in REQUIRED_CALLBACKS {
                if !instance.hasattr(callback)? {
                    return Err(pyo3::exceptions::PyAttributeError::new_err(format!(
                        "strategy {} must define {}",
                        spec.class, callback
                    )));
                }
            }
            Ok(LoadedStrategy {
                object: instance.unbind(),
                class: spec.class.clone(),
            })
        });
        result.map_err(HostError::from)
    }
}

/// A loaded, instantiated strategy bound to the host.
pub struct LoadedStrategy {
    object: Py<PyAny>,
    class: String,
}

impl LoadedStrategy {
    /// The loaded class name.
    #[must_use]
    pub fn class(&self) -> &str {
        &self.class
    }

    /// Invoke one callback with an optional JSON payload.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the payload is invalid or the callback raises.
    pub fn call(&self, callback: &str, payload_json: Option<&str>) -> Result<(), HostError> {
        let result: PyResult<()> = Python::attach(|py| {
            let bound = self.object.bind(py);
            match payload_json {
                Some(text) => {
                    let value = json_loads(py, text)?;
                    bound.call_method1(callback, (value,))?;
                }
                None => {
                    bound.call_method0(callback)?;
                }
            }
            Ok(())
        });
        result.map_err(HostError::from)
    }

    /// Invoke one callback and return its result encoded as JSON.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the payload is invalid, the callback raises,
    /// or its result cannot be encoded.
    pub fn call_json(&self, callback: &str, payload_json: Option<&str>) -> Result<String, HostError> {
        let result: PyResult<String> = Python::attach(|py| {
            let bound = self.object.bind(py);
            let value = match payload_json {
                Some(text) => bound.call_method1(callback, (json_loads(py, text)?,))?,
                None => bound.call_method0(callback)?,
            };
            json_dumps(py, &value)
        });
        result.map_err(HostError::from)
    }

    /// Dispatch a batch of callbacks under a single interpreter acquisition.
    ///
    /// This is the bounded-latency hot path: the GIL is taken once for the
    /// whole batch rather than once per event. Returns the number of
    /// dispatches whose callback existed on the strategy.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] if any payload is invalid or any callback raises.
    pub fn dispatch_batch(&self, events: &[StrategyEvent]) -> Result<usize, HostError> {
        let result: PyResult<usize> = Python::attach(|py| {
            let bound = self.object.bind(py);
            let mut dispatched = 0usize;
            for event in events {
                let value = json_loads(py, &event.payload_json)?;
                if bound.hasattr(event.callback.as_str())? {
                    bound.call_method1(event.callback.as_str(), (value,))?;
                    dispatched += 1;
                }
            }
            Ok(dispatched)
        });
        result.map_err(HostError::from)
    }

    /// Return whether the strategy defines a callback.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the attribute lookup raises.
    pub fn has_callback(&self, callback: &str) -> Result<bool, HostError> {
        let result: PyResult<bool> = Python::attach(|py| self.object.bind(py).hasattr(callback));
        result.map_err(HostError::from)
    }
}
