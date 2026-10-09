//! Native backtest execution, configuration identity, results, and numerical kernels.
//!
//! Execution and results are synchronous, seeded, and independent of wall-clock reads.

pub mod backtest;
pub mod canonical_json;
pub mod configuration_identity;
pub mod indicators;
pub mod results;
pub mod runtime;
pub mod sha256;
pub mod signal;

pub use canonical_json::JsonValue;
