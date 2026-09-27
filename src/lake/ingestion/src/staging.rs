//! In-memory staging writer over the schema-pinned Parquet serializer.
//!
//! [`StagingPartitionWriter`] plugs the real
//! [`ParquetWriter`](lake_storage::ParquetWriter) into a private in-memory
//! object store so one ingest can stage every selected dataset and hand the
//! complete objects to the caller's [`AtomicIngestCommitter`] in a single
//! commit. Staged bytes are never externally visible before that commit, and
//! `reset_staged` drops them without a trace.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, MutexGuard};

use lake_contracts::{AssetClass, DataError, DataType, Date};
use lake_storage::{BlobStore, ParquetMetadata, ParquetWriter};
use serde_json::Value;

use crate::common::{PartitionWriter, StagedParquetObject};

/// Private replacement-only store holding bytes until they are drained.
#[derive(Debug, Default)]
struct StagingStore {
    objects: Mutex<BTreeMap<String, Vec<u8>>>,
}

impl StagingStore {
    /// Remove and return one staged payload.
    fn take(&self, relative_path: &str) -> Option<Vec<u8>> {
        lock(&self.objects).remove(relative_path)
    }

    /// Drop every staged payload.
    fn clear(&self) {
        lock(&self.objects).clear();
    }
}

impl BlobStore for StagingStore {
    fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError> {
        lock(&self.objects).insert(relative_path.to_string(), payload.to_vec());
        Ok(())
    }
}

/// Shared staging writer; clone the [`Arc`] to share one staging area.
pub struct StagingPartitionWriter {
    store: Arc<StagingStore>,
    writer: ParquetWriter<StagingStore>,
    staged: Mutex<Vec<ParquetMetadata>>,
}

impl StagingPartitionWriter {
    /// Build an empty staging writer with its private in-memory store.
    pub fn new() -> Self {
        let store = Arc::new(StagingStore::default());
        let writer = ParquetWriter::new(Arc::clone(&store));
        Self {
            store,
            writer,
            staged: Mutex::new(Vec::new()),
        }
    }
}

impl Default for StagingPartitionWriter {
    fn default() -> Self {
        Self::new()
    }
}

impl PartitionWriter for StagingPartitionWriter {
    fn write(
        &self,
        records: &[Value],
        session_date: Date,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: &str,
    ) -> Result<ParquetMetadata, DataError> {
        let metadata = self
            .writer
            .write(records, session_date, asset_class, data_type, symbol)?;
        lock(&self.staged).push(metadata.clone());
        Ok(metadata)
    }

    fn drain_staged(&self) -> Vec<StagedParquetObject> {
        let metadata = std::mem::take(&mut *lock(&self.staged));
        metadata
            .into_iter()
            .map(|metadata| {
                let payload = self
                    .store
                    .take(&metadata.path)
                    .expect("the staging store holds every written object");
                StagedParquetObject { metadata, payload }
            })
            .collect()
    }

    fn reset_staged(&self) {
        lock(&self.staged).clear();
        self.store.clear();
    }
}

/// Lock a staging mutex, ignoring poisoning: the guarded data is plain bytes
/// and a panic elsewhere must not take the staging area down with it.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
#[path = "../tests/unit/staging.rs"]
mod tests;
