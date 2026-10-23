//! Owns live supervision, venue I/O and paper/shadow recovery composition.
//! Deterministic trading policy is supplied by harness-trading; no backtest dependency.

pub mod supervision;
pub mod trader;
pub mod venues;

pub use harness_trading::{LiveError, LiveResult};
pub mod runtime;
pub use num::Decimal18;
