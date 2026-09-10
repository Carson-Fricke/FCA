//! Deterministic backtest engine core.
//!
//! This module ports the deterministic, pure components of
//! `harness.runtime.backtest` (errors, fill selection, fee derivation, fixtures,
//! preflight, and the reproducibility manifest) and exposes the engine loop
//! through traits. The full Python engine composes the strategy runtime, the
//! risk pipeline, the portfolio hub, and the simulated venue; those capabilities
//! live in other migration epics, so [`engine`] consumes them through clean
//! seams that are exercised with deterministic fakes.

pub mod config;
pub mod engine;
pub mod errors;
pub mod fees;
pub mod fills;
pub mod fixture;
pub mod manifest;
pub mod preflight;
pub mod rng;
