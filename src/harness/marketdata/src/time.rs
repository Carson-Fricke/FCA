//! UTC calendar primitives shared with the market-lake contract.
//!
//! The harness and the lake exchange one [`Date`]/[`DateTime`]/[`Nanos`]
//! timeline; this module keeps the `marketdata::time` import path while the
//! definitions live in `lake-contracts`.

pub use lake_contracts::time::*;
