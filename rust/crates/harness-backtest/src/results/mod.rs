//! Results recording, metrics, and report kernels for the backtest engine.
//!
//! The first resident is the streaming results recorder
//! ([`recorder`]) ported from `harness.results.recorder`. Metrics, reports, and
//! the results store follow in the backtest-job task.

pub mod recorder;
