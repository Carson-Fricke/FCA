//! Object/catalog publication for the provider-backed ingest transactions.
//!
//! Ports `lake.ingestion.azure`: publishes immutable objects and atomically exposes their
//! coverage through the shared fenced manifest catalog.

use std::sync::Arc;

use lake_catalog::{CoverageRecord, PublicationCatalog};
use lake_contracts::DataError;
use lake_storage::BlobStore;

use crate::common::{AtomicIngestCommitter, StagedParquetObject};

/// Publish verified immutable objects through one fenced symbol-day manifest.
pub struct ObjectCatalogCommitter {
    object_store: Arc<dyn BlobStore>,
    catalog: Arc<PublicationCatalog>,
}

impl ObjectCatalogCommitter {
    /// Build the committer over an injected blob store and catalog.
    pub fn new(object_store: Arc<dyn BlobStore>, catalog: Arc<PublicationCatalog>) -> Self {
        Self {
            object_store,
            catalog,
        }
    }
}

impl AtomicIngestCommitter for ObjectCatalogCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        let pipeline = lake_pipeline::Pipeline::global();
        let cpu = pipeline.cpu_blocking()?;
        let mut validation = pipeline.timer(lake_pipeline::Stage::Hash);
        validation.bytes(objects.iter().map(|object| object.payload.len()).sum());
        let mut paths = std::collections::BTreeSet::new();
        for object in objects {
            let digest = object.payload.checksum()?;
            if !paths.insert(object.metadata.path.as_str())
                || object.metadata.bytes != object.payload.len() as u64
                || object.metadata.checksum != digest
                || lake_contracts::object_checksum(&object.metadata.path)? != digest
                || !coverage.iter().any(|r| {
                    r.object_path.as_deref() == Some(&object.metadata.path)
                        && r.checksum.as_deref() == Some(digest.as_str())
                        && r.byte_size == object.payload.len() as u64
                        && r.row_count == object.metadata.rows
                })
            {
                return Err(DataError::value(
                    "staged object does not match publication metadata",
                ));
            }
        }
        if coverage.iter().filter(|r| r.object_path.is_some()).count() != objects.len() {
            return Err(DataError::value(
                "publication must stage every referenced object exactly once",
            ));
        }
        drop(validation);
        drop(cpu);
        self.catalog.publish(coverage, || {
            for staged in objects {
                let pipeline = lake_pipeline::Pipeline::global();
                pipeline.check()?;
                let mut upload = pipeline.timer(lake_pipeline::Stage::Upload);
                upload.bytes(staged.payload.len());
                self.object_store
                    .replace_file(&staged.metadata.path, staged.payload.path())?;
            }
            Ok(())
        })
    }
}
