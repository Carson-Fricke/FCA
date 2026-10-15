//! Failures at the snapshot and partition-read boundary.

#[derive(Debug, thiserror::Error)]
pub enum ReaderError {
    #[error("invalid market-data snapshot: {0}")]
    Snapshot(String),
    #[error("market-data partition is unavailable: {0}")]
    Source(String),
    #[error("market-data partition does not match its snapshot: {0}")]
    Integrity(String),
    #[error("market-data cache: {0}")]
    Cache(String),
    #[error("market-data decode: {0}")]
    Decode(String),
}
