//! Harness-owned, read-only market-data access.
//!
//! Catalog/admission services produce immutable manifests. This capability
//! verifies and reads their Parquet objects through an injected read-only
//! source, and reuses verified bytes and Arrow batches within bounded caches.
//! Provider acquisition, job dispatch, and simulation policy live elsewhere.

mod cache;
mod config;
mod error;
mod reader;
mod snapshot;

pub use config::CacheConfig;
pub use error::ReaderError;
pub use reader::{MarketDataReader, PartitionSource};
pub use snapshot::{MarketDataSnapshot, Partition, SnapshotRequest};
