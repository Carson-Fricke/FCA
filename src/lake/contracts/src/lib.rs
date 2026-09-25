//! Stable market-lake contracts.
//!
//! Asset classes, data types, Hive partition paths, the pinned Arrow schemas,
//! the exchange/UTC session calendars, symbol canonicalisation, the fixed-point
//! decimal value, and the shared data-layer error. Everything the lake service
//! and its consumers agree on lives here and changes only with a schema-version
//! bump.

pub mod decimal;
pub mod error;
pub mod partitions;
pub mod schemas;
pub mod sessions;
pub mod symbol;
pub mod time;

pub use decimal::{Decimal18, DecimalError};
pub use error::DataError;
pub use partitions::{
    partition_path, symbol_filename, AssetClass, DataType, Dataset, SUPPORTED_DATASETS,
};
pub use schemas::{
    schema_for, schema_name, SCHEMA_METADATA_KEY, SCHEMA_VERSION, SCHEMA_VERSION_METADATA_KEY,
};
pub use sessions::{
    crypto_sessions, crypto_utc_days, is_partial, sessions_for, xnys_sessions, ExchangeSession,
};
pub use symbol::{canonical_osi_symbol, canonical_symbol};
pub use time::{format_nanos_utc, parse_iso_nanos, Date, DateTime, Nanos};
