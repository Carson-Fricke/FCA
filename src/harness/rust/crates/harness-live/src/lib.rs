//! Deterministic live-trading core for the quant-harness Rust engine.
//!
//! This crate is the staged Rust port of the Python live-trading surface under
//! `src/harness`: order management, fill and fee models, venue adapters,
//! portfolio accounting, and the risk engine. The synchronous kernel is
//! deterministic — no wall-clock or network access — while venue I/O is
//! isolated behind traits and `tokio`.
//!
//! The Python engine and the pinned native core remain authoritative until the
//! migration flips.

pub mod accounting;
pub mod error;
pub mod events;
pub mod fills;
pub mod oms;
pub mod orders;
pub mod portfolio;
pub mod risk;
pub mod symbol;
pub mod trader;
pub mod venues;

pub use error::{LiveError, LiveResult};
pub use harness_num::Decimal18;
