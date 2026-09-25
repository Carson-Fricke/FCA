//! Lease-protected atomic ingest composition.
//!
//! Ports `lake.ingestion.leased`: derive the one symbol-day an atomic commit
//! protects and hold its exclusive lease across the commit.

use std::sync::Arc;

use lake_catalog::CoverageRecord;
use lake_contracts::DataError;
use lake_storage::{LeaseProvider, SymbolDayScope};

use crate::common::{AtomicIngestCommitter, StagedParquetObject};

/// Derive and validate the one symbol-day protected by an atomic commit.
pub fn scope_for_coverage(coverage: &[CoverageRecord]) -> Result<SymbolDayScope, DataError> {
    let Some(first) = coverage.first() else {
        return Err(DataError::value(
            "a leased atomic commit requires coverage records",
        ));
    };
    for record in &coverage[1..] {
        if (
            record.asset_class,
            record.symbol.as_str(),
            record.session_date,
        ) != (first.asset_class, first.symbol.as_str(), first.session_date)
        {
            return Err(DataError::value(
                "a leased atomic commit must contain one symbol-day",
            ));
        }
    }
    SymbolDayScope::new(first.asset_class, &first.symbol, first.session_date)
}

/// Protect an atomic object/catalog commit with one symbol-day lease.
pub struct LeasedAtomicIngestCommitter {
    committer: Arc<dyn AtomicIngestCommitter>,
    leases: Arc<dyn LeaseProvider>,
}

impl LeasedAtomicIngestCommitter {
    /// Build the committer over an injected inner committer and lease source.
    pub fn new(committer: Arc<dyn AtomicIngestCommitter>, leases: Arc<dyn LeaseProvider>) -> Self {
        Self { committer, leases }
    }
}

impl AtomicIngestCommitter for LeasedAtomicIngestCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        let scope = scope_for_coverage(coverage)?;
        let lease = self.leases.acquire(&scope)?;
        // The commit error wins over a release error so the publisher's
        // failure surfaces first; a successful commit still propagates a
        // release failure instead of silently dropping it.
        let committed = self.committer.commit(objects, coverage);
        let released = lease.release();
        match (committed, released) {
            (Err(error), _) | (Ok(()), Err(error)) => Err(error),
            (Ok(()), Ok(())) => Ok(()),
        }
    }
}
