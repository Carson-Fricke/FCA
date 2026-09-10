//! Host for user-authored screener scripts.
//!
//! A screener program is a Python source file that defines a module-level
//! `screen` callable. The host executes the file, resolves `screen`, and
//! invokes it with a JSON context, returning the JSON-encoded result. This is
//! the host surface only; the engine-side screening package lands separately.

use std::ffi::CString;
use std::path::{Path, PathBuf};

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::error::HostError;
use crate::python::{install_search_paths, json_dumps, json_loads};

/// The entry-point callable every screener program defines.
pub const SCREEN_ENTRYPOINT: &str = "screen";

/// Host that executes user screener programs.
#[derive(Debug, Clone, Default)]
pub struct ScreenerHost {
    search_paths: Vec<PathBuf>,
}

impl ScreenerHost {
    /// Construct a host with no extra search paths.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Construct a host that prepends `paths` to `sys.path`.
    #[must_use]
    pub fn with_search_paths(paths: Vec<PathBuf>) -> Self {
        Self { search_paths: paths }
    }

    /// Execute one screener source file and resolve its `screen` callable.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the file cannot be read, does not compile,
    /// raises while executing, or does not define a callable `screen`.
    pub fn load_program(&self, path: &Path) -> Result<LoadedScreener, HostError> {
        let source = std::fs::read_to_string(path)?;
        let code = CString::new(source)
            .map_err(|error| HostError::Invalid(format!("screener source: {error}")))?;
        let path = path.to_path_buf();
        let search_paths = self.search_paths.clone();
        let result: PyResult<LoadedScreener> = Python::attach(|py| {
            install_search_paths(py, &search_paths)?;
            let globals = PyDict::new(py);
            py.run(code.as_c_str(), Some(&globals), None)?;
            let screen = globals
                .get_item(SCREEN_ENTRYPOINT)?
                .ok_or_else(|| {
                    pyo3::exceptions::PyAttributeError::new_err(format!(
                        "screener program must define {SCREEN_ENTRYPOINT}"
                    ))
                })?;
            if !screen.is_callable() {
                return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                    "{SCREEN_ENTRYPOINT} must be callable"
                )));
            }
            Ok(LoadedScreener {
                screen: screen.unbind(),
                source: path,
            })
        });
        result.map_err(HostError::from)
    }
}

/// A loaded screener program bound to the host.
pub struct LoadedScreener {
    screen: Py<PyAny>,
    source: PathBuf,
}

impl LoadedScreener {
    /// The source file path.
    #[must_use]
    pub fn source(&self) -> &Path {
        &self.source
    }

    /// Run the screener against one JSON context, returning JSON.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the context is invalid, the program raises,
    /// or its result cannot be encoded.
    pub fn run(&self, context_json: &str) -> Result<String, HostError> {
        let result: PyResult<String> = Python::attach(|py| {
            let bound = self.screen.bind(py);
            let context = json_loads(py, context_json)?;
            let output = bound.call1((context,))?;
            json_dumps(py, &output)
        });
        result.map_err(HostError::from)
    }
}
