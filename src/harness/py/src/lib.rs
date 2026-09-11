//! Python bindings for the quant-harness Rust engine.
//!
//! This crate is the PyO3 bridge between the Python engine under
//! `src/harness` and the staged Rust core. It currently exposes a single
//! smoke-test surface, [`smoke_roundtrip`], so the load path can be exercised
//! end to end before the real strategy host and parity oracle land.
//!
//! The Python engine and the pinned Zig native core remain authoritative until
//! the migration flips. This module is additive: it does not replace the
//! `harness._native` ctypes loader.

use pyo3::prelude::*;

/// Round-trip an integer through the Rust core.
///
/// This is the minimal smoke-test surface for the PyO3 bridge. It is a plain
/// Rust function so it can be unit-tested without a Python interpreter; the
/// `#[pyfunction]` wrapper below exposes it to Python.
pub fn smoke_roundtrip(value: i64) -> i64 {
    value
}

#[pyfunction]
#[pyo3(name = "smoke_roundtrip")]
fn py_smoke_roundtrip(value: i64) -> i64 {
    smoke_roundtrip(value)
}

/// The Python module exposed as ``harness._rust``.
#[pymodule]
fn _rust(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(py_smoke_roundtrip, module)?)?;
    Ok(())
}

#[cfg(test)]
#[path = "../tests/unit/lib.rs"]
mod tests;
