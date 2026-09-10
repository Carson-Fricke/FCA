//! Backtest, sweeps, and results kernels for the quant-harness Rust port.
//!
//! The Python engine under `src/harness` remains authoritative until the
//! migration flips. This crate is the staged Rust home for the backtest epic
//! (T238): the results recorder (T259), the deterministic backtest engine core
//! (T260), paper/shadow runtimes (T261), the sweeps engine (T262), built-in
//! signal and indicator kernels (T263), and the end-to-end backtest job (T264).
//!
//! Every kernel in this crate is synchronous and free of wall-clock reads,
//! network access, and unseeded randomness. Where the Rust native core
//! (`harness-native`) or the Rust data layer is not yet available, the crate
//! exposes clean traits and is tested with deterministic fakes.

pub mod backtest;
pub mod canonical_json;
pub mod results;
pub mod runtime;
pub mod sha256;

pub use canonical_json::JsonValue;
