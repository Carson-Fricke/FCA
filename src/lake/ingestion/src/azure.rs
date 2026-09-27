//! Azure composition for the provider-backed ingest transactions.
//!
//! Ports `lake.ingestion.azure`: publishes deterministic objects and their
//! catalog rows through the replace-only blob boundary and the coverage
//! catalog port.

use std::sync::Arc;

use lake_catalog::{CatalogRepository, CoverageRecord};
use lake_contracts::DataError;
use lake_storage::BlobStore;

use crate::common::{AtomicIngestCommitter, StagedParquetObject};

/// Publish deterministic objects and their catalog rows to Azure.
pub struct AzureAtomicIngestCommitter {
    object_store: Arc<dyn BlobStore>,
    catalog: Arc<dyn CatalogRepository>,
}

impl AzureAtomicIngestCommitter {
    /// Build the committer over an injected blob store and catalog.
    pub fn new(object_store: Arc<dyn BlobStore>, catalog: Arc<dyn CatalogRepository>) -> Self {
        Self {
            object_store,
            catalog,
        }
    }
}

impl AtomicIngestCommitter for AzureAtomicIngestCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        // Replace all object payloads before publishing matching coverage rows.
        for staged in objects {
            self.object_store
                .replace(&staged.metadata.path, &staged.payload)?;
        }
        for record in coverage {
            self.catalog.upsert(record)?;
        }
        Ok(())
    }
}
