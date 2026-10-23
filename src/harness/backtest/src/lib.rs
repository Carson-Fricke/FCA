//! Native backtest execution, configuration identity, results, and numerical kernels.
//!
//! Execution and results are synchronous, seeded, and independent of wall-clock reads.

pub mod backtest;
pub mod configuration_identity;
pub mod indicators;
pub mod results;
pub mod signal;

pub use harness_contracts::canonical_json::JsonValue;
