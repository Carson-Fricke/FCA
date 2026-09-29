//! Market-lake session and partition vocabulary.
//!
//! The harness and the lake agree on asset classes, data types, the XNYS and
//! UTC-day calendars, and the canonical Hive partition naming through
//! `lake-contracts`; this module re-exports that vocabulary under the harness
//! module path. Route traffic lives in `harness-lakeclient`: the harness reads
//! through the frozen HTTP contract and never opens lake files.

pub use lake_contracts::{
    crypto_sessions, crypto_utc_days, partition_path, sessions_for, symbol_filename, xnys_sessions,
    AssetClass, BarInterval, DataType, ExchangeSession,
};

#[cfg(test)]
#[path = "../tests/unit/lake.rs"]
mod tests;
