//! Results recording, metrics, and report kernels for the backtest engine.
//!
//! The first residents are the streaming results recorder ([`recorder`]), the
//! pure deterministic metrics ([`metrics`]), and the offline HTML report
//! ([`report`]) ported from `harness.results`.

pub mod metrics;
pub mod recorder;
pub mod report;
