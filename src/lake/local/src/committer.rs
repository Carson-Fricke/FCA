//! Local commit boundaries: objects first, then coverage.
//!
//! Ports `LocalIngestCommitter` and `_BackfillCommitter` from
//! `lake/local/ingest.py`: a one-shot committer publishes every staged object
//! through the local blob store before upserting coverage, and a backfill
//! committer writes objects through on every commit while buffering catalog
//! rows until a batch size is reached or the run ends.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, MutexGuard};

use lake_catalog::{CatalogRepository, CoverageRecord, JsonCatalogStore};
use lake_contracts::{AssetClass, DataError, DataType, Date};
use lake_ingestion::{AtomicIngestCommitter, StagedParquetObject};
use lake_storage::BlobStore;

type CoverageKey = (AssetClass, DataType, String, Date);

/// Publish local objects before atomically replacing catalog JSON.
pub struct LocalIngestCommitter {
    store: Arc<dyn BlobStore>,
    catalog: Arc<dyn CatalogRepository>,
}

impl LocalIngestCommitter {
    /// Build a committer over an object store and a coverage catalog.
    pub fn new(store: Arc<dyn BlobStore>, catalog: Arc<dyn CatalogRepository>) -> Self {
        Self { store, catalog }
    }
}

impl AtomicIngestCommitter for LocalIngestCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        for staged in objects {
            self.store.replace(&staged.metadata.path, &staged.payload)?;
        }
        for record in coverage {
            self.catalog.upsert(record)?;
        }
        Ok(())
    }
}

/// Publish objects per session while batching the catalog persistence.
///
/// Parquet objects are written through on every commit; only the catalog
/// rewrite (full read + fsync + atomic replace of the whole file) is buffered
/// and flushed in batches. A crash before a flush loses catalog rows but not
/// objects: coverage-driven resume re-fetches those uncommitted sessions
/// deterministically, so an interrupted backfill is never published as
/// complete.
pub(crate) struct LocalBackfillCommitter {
    store: Arc<dyn BlobStore>,
    catalog: Arc<JsonCatalogStore>,
    flush_records: usize,
    pending: Mutex<BTreeMap<CoverageKey, CoverageRecord>>,
}

impl LocalBackfillCommitter {
    /// Build a buffering committer with the supplied catalog flush threshold.
    pub(crate) fn new(
        store: Arc<dyn BlobStore>,
        catalog: Arc<JsonCatalogStore>,
        flush_records: usize,
    ) -> Self {
        Self {
            store,
            catalog,
            flush_records,
            pending: Mutex::new(BTreeMap::new()),
        }
    }

    /// Persist every buffered coverage row in one catalog update.
    ///
    /// Rows are keyed by their logical coverage key, so a retry that replaces
    /// an earlier error row with a complete one overwrites it in place and the
    /// single flushed update never carries duplicate keys.
    pub(crate) fn flush(&self) -> Result<(), DataError> {
        let mut pending = lock(&self.pending);
        if pending.is_empty() {
            return Ok(());
        }
        let records: Vec<CoverageRecord> = pending.values().cloned().collect();
        self.catalog.upsert_many(records.iter())?;
        pending.clear();
        Ok(())
    }
}

impl AtomicIngestCommitter for LocalBackfillCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        for staged in objects {
            self.store.replace(&staged.metadata.path, &staged.payload)?;
        }
        let mut pending = lock(&self.pending);
        for record in coverage {
            pending.insert(record.key(), record.clone());
        }
        if pending.len() >= self.flush_records {
            let records: Vec<CoverageRecord> = pending.values().cloned().collect();
            self.catalog.upsert_many(records.iter())?;
            pending.clear();
        }
        Ok(())
    }
}

/// Lock a committer mutex, ignoring poisoning: the buffered rows are plain
/// data and a panic elsewhere must not take the committer down with it.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
#[path = "../tests/unit/committer.rs"]
mod tests;
