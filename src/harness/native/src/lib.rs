//! Native core for the quant-harness Rust engine.
//!
//! This crate hosts the Rust port of the pinned Zig native core under
//! `src/harness/native`. The C ABI surface consumed by `src/harness/_native.py`
//! lives in [`capi`], and the deterministic domain kernels are exposed as
//! focused modules:
//!
//! - [`types`]: canonical symbols, deterministic ids, canonical decimal text,
//!   UTC ISO-8601 timestamps, and canonical trade-row serialization.
//! - [`decimal`]: the pinned scale-18 fixed-point kernel
//!   (`decimal128(38, 18)`).
//! - [`clock`]: the deterministic `SimClock`.
//! - [`market`]: quote/trade/order domain records and the event-class
//!   priority table.
//! - [`simple_touch`] and [`latency_size`]: the deterministic fill models.
//!
//! Determinism rules: no wall-clock, no unseeded random, no network. Errors are
//! returned, never panicked, so no panic can cross the FFI boundary.

pub mod arrow;
pub mod bars;
pub mod books;
pub mod capi;
pub mod clock;
pub mod decimal;
pub mod fills;
pub mod kernel;
pub mod latency_size;
pub mod market;
pub mod market_bus;
pub mod matching;
pub mod metrics;
pub mod oms;
pub mod quant;
mod sha256;
pub mod simple_touch;
pub mod trade_through;
pub mod types;
mod wide;

/// Returns the semantic version of this crate as a static string.
pub const fn version() -> &'static str {
    "0.1.0"
}
