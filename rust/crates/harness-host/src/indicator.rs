//! Host for user-authored Python indicators.
//!
//! An indicator is any module-level callable that accepts a sequence of
//! values and returns a scalar or a sequence. The host batches calls under a
//! single interpreter acquisition so a hot indicator loop never pays a
//! per-call GIL acquisition.

use std::path::PathBuf;

use pyo3::prelude::*;

use crate::error::HostError;
use crate::python::{install_search_paths, json_loads};

/// Host that imports user indicator callables.
#[derive(Debug, Clone, Default)]
pub struct IndicatorHost {
    search_paths: Vec<PathBuf>,
}

impl IndicatorHost {
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

    /// Import one module-level indicator callable.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the module or callable is missing.
    pub fn load(&self, module: &str, attribute: &str) -> Result<LoadedCallable, HostError> {
        let search_paths = self.search_paths.clone();
        let result: PyResult<LoadedCallable> = Python::attach(|py| {
            install_search_paths(py, &search_paths)?;
            let module = py.import(module)?;
            let callable = module.getattr(attribute)?;
            if !callable.is_callable() {
                return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                    "{attribute} must be callable"
                )));
            }
            Ok(LoadedCallable {
                callable: callable.unbind(),
                module: module.name()?.to_string(),
                attribute: attribute.to_string(),
            })
        });
        result.map_err(HostError::from)
    }
}

/// A loaded callable bound to the host.
pub struct LoadedCallable {
    callable: Py<PyAny>,
    module: String,
    attribute: String,
}

impl LoadedCallable {
    /// The source module name.
    #[must_use]
    pub fn module(&self) -> &str {
        &self.module
    }

    /// The source attribute name.
    #[must_use]
    pub fn attribute(&self) -> &str {
        &self.attribute
    }

    /// Call the indicator with one slice of inputs, returning a sequence.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the callable raises or returns a non-numeric
    /// value.
    pub fn call_f64(&self, inputs: &[f64]) -> Result<Vec<f64>, HostError> {
        let result: PyResult<Vec<f64>> = Python::attach(|py| {
            let bound = self.callable.bind(py);
            let value = bound.call1((inputs.to_vec(),))?;
            extract_f64_series(&value)
        });
        result.map_err(HostError::from)
    }

    /// Call the indicator once per slice under a single interpreter
    /// acquisition.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when any call raises or returns a non-numeric
    /// value.
    pub fn call_f64_batch(&self, batches: &[Vec<f64>]) -> Result<Vec<Vec<f64>>, HostError> {
        let result: PyResult<Vec<Vec<f64>>> = Python::attach(|py| {
            let bound = self.callable.bind(py);
            let mut outputs = Vec::with_capacity(batches.len());
            for inputs in batches {
                let value = bound.call1((inputs.clone(),))?;
                outputs.push(extract_f64_series(&value)?);
            }
            Ok(outputs)
        });
        result.map_err(HostError::from)
    }
}

fn extract_f64_series(value: &Bound<'_, PyAny>) -> PyResult<Vec<f64>> {
    if let Ok(values) = value.extract::<Vec<f64>>() {
        return Ok(values);
    }
    let scalar: f64 = value.extract()?;
    Ok(vec![scalar])
}

/// Decode a JSON-encoded parameter mapping into Python keyword arguments.
///
/// Retained as a host utility for indicator parameterisation.
///
/// # Errors
///
/// Returns [`HostError`] when the JSON payload is invalid.
pub fn decode_parameters<'py>(
    py: Python<'py>,
    parameters_json: &str,
) -> Result<Bound<'py, PyAny>, HostError> {
    json_loads(py, parameters_json).map_err(HostError::from)
}
