//! Explicit retained-cache budgets, independent of ingestion worker sizing.

use crate::ReaderError;
use std::path::PathBuf;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CacheConfig {
    pub directory: PathBuf,
    pub disk_bytes: u64,
    pub memory_bytes: usize,
}

impl Default for CacheConfig {
    fn default() -> Self {
        Self {
            directory: PathBuf::from(".local/market-data-cache"),
            disk_bytes: 4 * 1024 * 1024 * 1024,
            memory_bytes: 512 * 1024 * 1024,
        }
    }
}

impl CacheConfig {
    pub fn from_env() -> Result<Self, ReaderError> {
        let mut config = Self::default();
        if let Some(value) = std::env::var_os("HARNESS_MARKET_DATA_CACHE_DIR") {
            config.directory = value.into();
        }
        if let Ok(value) = std::env::var("HARNESS_MARKET_DATA_CACHE_DISK_BYTES") {
            config.disk_bytes = value
                .parse()
                .map_err(|_| ReaderError::Cache("invalid disk budget".into()))?;
        }
        if let Ok(value) = std::env::var("HARNESS_MARKET_DATA_CACHE_MEMORY_BYTES") {
            config.memory_bytes = value
                .parse()
                .map_err(|_| ReaderError::Cache("invalid memory budget".into()))?;
        }
        Ok(config)
    }
}
