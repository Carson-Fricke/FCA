//! Shared Python interop helpers for the host.
//!
//! The host embeds one interpreter through PyO3's `auto-initialize` feature.
//! Every call site acquires the interpreter exactly once and does all of its
//! Python work inside that single attach, so hot loops never pay a
//! per-callback GIL acquisition.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyModule, PyTuple};
use serde_json::Value as JsonValue;

/// Initialize the embedded interpreter on the calling thread.
///
/// PyO3's `auto-initialize` initializes the interpreter on the first
/// `Python::attach`, and on Windows that initialization deadlocks when it runs
/// on a spawned background thread. Serving processes therefore warm the
/// interpreter on the main thread before handing work to any worker thread;
/// later attaches only acquire the GIL.
pub fn warm_interpreter() {
    Python::attach(|_| {});
}

/// Serializes every embedded-Python engine run in this process.
///
/// The interpreter, the `harness.data` shim, and the first user-code imports are
/// process-global. Concurrent engine runs can otherwise race the first import of
/// a shared module and observe it partially initialized, so every engine entry
/// point acquires this single lock for the duration of one run.
pub static PYTHON_ENGINE_LOCK: Mutex<()> = Mutex::new(());

/// Insert search paths at the front of `sys.path` in order.
///
/// # Errors
///
/// Returns the Python error when `sys.path` cannot be mutated.
pub fn install_search_paths(py: Python<'_>, paths: &[PathBuf]) -> PyResult<()> {
    if paths.is_empty() {
        return Ok(());
    }
    let sys = PyModule::import(py, "sys")?;
    let path = sys.getattr("path")?;
    for candidate in paths.iter().rev() {
        let value = candidate.to_string_lossy().to_string();
        path.call_method1("insert", (0, value))?;
    }
    Ok(())
}

/// Convert one path to a UTF-8 string, failing on non-UTF-8 paths.
///
/// # Errors
///
/// Returns a Python error when the path is not valid UTF-8.
pub fn path_to_string(path: &Path) -> PyResult<String> {
    path.to_str()
        .map(str::to_string)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("path is not valid UTF-8"))
}

/// Decode a JSON document into a Python object.
///
/// # Errors
///
/// Returns the Python error raised by `json.loads`.
pub fn json_loads<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyAny>> {
    let json = PyModule::import(py, "json")?;
    json.call_method1("loads", (text,))
}

/// Encode a Python object as a compact JSON document.
///
/// # Errors
///
/// Returns the Python error raised by `json.dumps` or extraction.
pub fn json_dumps(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<String> {
    let json = PyModule::import(py, "json")?;
    json.call_method1("dumps", (value,))?.extract()
}

/// The repository root that contains `src/` and `.venv/`, when the host is
/// running from a source checkout.
///
/// Returns `None` for an installed wheel, where `harness` resolves through the
/// ambient interpreter instead of the checkout.
#[must_use]
pub fn workspace_root() -> Option<PathBuf> {
    let mut current = Path::new(env!("CARGO_MANIFEST_DIR"));
    while let Some(parent) = current.parent() {
        if parent.join("src").join("harness").is_dir() && parent.join("pyproject.toml").is_file() {
            return Some(parent.to_path_buf());
        }
        current = parent;
    }
    None
}

/// Default import paths for the embedded interpreter.
///
/// A source checkout exposes the `harness` package under `<root>/src` and its
/// installed dependencies under `<root>/.venv`. An installed distribution
/// resolves `harness` through the ambient interpreter, so directories that do
/// not exist are skipped.
#[must_use]
pub fn default_search_paths() -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if let Some(root) = workspace_root() {
        let source = root.join("src");
        if source.is_dir() {
            paths.push(source);
        }
        let site_packages = root.join(".venv").join("Lib").join("site-packages");
        if site_packages.is_dir() {
            paths.push(site_packages);
        }
    }
    if let Ok(virtual_env) = std::env::var("VIRTUAL_ENV") {
        let site_packages = Path::new(&virtual_env).join("Lib").join("site-packages");
        if site_packages.is_dir() {
            paths.push(site_packages);
        }
    }
    if let Ok(python) = std::env::var("PYO3_PYTHON") {
        let interpreter = Path::new(&python);
        if let Some(scripts) = interpreter.parent() {
            if let Some(venv) = scripts.parent() {
                let site_packages = venv.join("Lib").join("site-packages");
                if site_packages.is_dir() {
                    paths.push(site_packages);
                }
            }
        }
    }
    paths
}

/// Convert a Python object into a JSON value without invoking strategy code.
///
/// Mappings (including `MappingProxyType`), lists, tuples, `Decimal`, and the
/// scalar types are supported. Unsupported objects fall back to their `str`
/// rendering so the conversion never raises on a frozen state value.
///
/// # Errors
///
/// Returns the Python error when an attribute lookup or conversion fails.
pub fn to_json_value(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<JsonValue> {
    if value.is_none() {
        return Ok(JsonValue::Null);
    }
    if let Ok(boolean) = value.extract::<bool>() {
        return Ok(JsonValue::Bool(boolean));
    }
    if let Ok(integer) = value.extract::<i64>() {
        return Ok(JsonValue::from(integer));
    }
    if let Ok(text) = value.extract::<String>() {
        return Ok(JsonValue::String(text));
    }
    let type_name = value.get_type().name()?.to_string();
    if type_name == "Decimal" {
        return Ok(JsonValue::String(value.str()?.to_string()));
    }
    if let Ok(float) = value.extract::<f64>() {
        return Ok(serde_json::Number::from_f64(float).map_or(JsonValue::Null, JsonValue::Number));
    }
    if value.hasattr("items")? {
        let dict = PyModule::import(py, "builtins")?
            .getattr("dict")?
            .call1((value,))?;
        let dict = dict.cast::<PyDict>()?;
        let mut object = serde_json::Map::new();
        for (key, item) in dict.iter() {
            object.insert(key.extract::<String>()?, to_json_value(py, &item)?);
        }
        return Ok(JsonValue::Object(object));
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        let mut array = Vec::with_capacity(tuple.len());
        for item in tuple.iter() {
            array.push(to_json_value(py, &item)?);
        }
        return Ok(JsonValue::Array(array));
    }
    if let Ok(list) = value.cast::<PyList>() {
        let mut array = Vec::with_capacity(list.len());
        for item in list.iter() {
            array.push(to_json_value(py, &item)?);
        }
        return Ok(JsonValue::Array(array));
    }
    Ok(JsonValue::String(value.str()?.to_string()))
}

/// Encode a Python object as compact JSON using [`to_json_value`].
///
/// # Errors
///
/// Returns the Python error when conversion fails or the JSON cannot be
/// serialized.
pub fn json_dumps_value(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<String> {
    serde_json::to_string(&to_json_value(py, value)?)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}

/// Pre-seed `sys.modules` so importing `harness.data` submodules does not run
/// the package `__init__`.
///
/// `harness.data.__init__` eagerly imports the market-lake client, which loads
/// the embedded DuckDB library. DuckDB's native shutdown path faults when the
/// interpreter is embedded and never finalized, so the host installs a lazy
/// package shim and loads only the submodules the strategy context needs. The
/// shim is idempotent and preserves the real `__path__`, so every
/// strategy-relevant submodule still resolves normally.
///
/// # Errors
///
/// Returns the Python error when `sys.modules` cannot be inspected or the
/// shimmed module cannot be created.
pub fn install_marketdata_shim(py: Python<'_>) -> PyResult<()> {
    let sys = PyModule::import(py, "sys")?;
    let modules = sys.getattr("modules")?;
    if modules.contains("harness.data")? {
        return Ok(());
    }
    let spec =
        PyModule::import(py, "importlib.util")?.call_method1("find_spec", ("harness.data",))?;
    if spec.is_none() {
        return Ok(());
    }
    let module = PyModule::new(py, "harness.data")?;
    module.setattr("__path__", spec.getattr("submodule_search_locations")?)?;
    module.setattr("__package__", "harness.data")?;
    module.setattr(
        "__doc__",
        "Lazy host shim: submodules load without the market-lake package init.",
    )?;
    modules.set_item("harness.data", module)?;
    Ok(())
}
