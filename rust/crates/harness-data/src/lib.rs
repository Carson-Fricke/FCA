//! Historical and live market-data layer for the quant-harness Rust engine.
//!
//! This crate is the staged Rust port of `src/harness/data`. It follows the
//! Python module boundaries so behavior and on-disk contracts can be checked
//! one module at a time:
//!
//! - [`lake`] — synchronous market-lake access, sessions, and Hive paths.
//! - [`bars`] — exact OHLCV derivation from immutable trade events.
//! - [`history`] — bounded point-in-time history with look-ahead clipping.
//!
//! No code path in this crate reads wall time, consumes randomness, touches
//! the network, or performs I/O. Every instant comes from an injected clock.

pub mod bars;
pub mod catalog;
pub mod clock;
pub mod error;
pub mod events;
pub mod gap_healing;
pub mod history;
pub mod lake;
pub mod listing_events;
pub mod options_replay;
pub mod options_universe;
pub mod replay;
pub mod security_master;
pub mod symbol;
pub mod time;
pub mod universe_view;

pub use error::DataError;
pub use time::{Date, DateTime, Nanos};

/// The persisted market-data schema version shared with `lake.contracts`.
pub const SCHEMA_VERSION: &str = "1";
