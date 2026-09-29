//! Canonical market-symbol validation shared with the market-lake contract.
//!
//! Symbols are uppercased and stripped, must be non-empty, and must match the
//! contract's market-symbol character rule; compact OSI option symbols and
//! dashed crypto pairs resolve through the same contract this module
//! re-exports.

pub use lake_contracts::symbol::*;
