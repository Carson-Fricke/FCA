//! Shared Python interop helpers for the host.
//!
//! The host embeds one interpreter through PyO3's `auto-initialize` feature.
//! Every call site acquires the interpreter exactly once and does all of its
//! Python work inside that single attach, so hot loops never pay a
//! per-callback GIL acquisition.

use std::path::{Path, PathBuf};

use pyo3::prelude::*;
use pyo3::types::PyModule;

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
