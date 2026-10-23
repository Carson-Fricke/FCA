//! Owns deterministic trading policy, validated order types and accounting.
//! Replay/live hosts supply time, market events and effects; no I/O belongs here.

pub mod accounting;
pub mod error;
pub mod events;
pub mod fills;
pub mod ledger;
pub mod matching;
pub mod oms;
pub mod orders;
pub mod portfolio;
pub mod replay_errors;
pub mod replay_fills;
pub mod risk;
pub mod symbol;

pub use error::{LiveError, LiveResult};

pub mod program;

pub mod fee_policy;

pub mod notional_policy;
