//! Lease-protected atomic ingest composition.
//!
//! Ports `lake.ingestion.leased`: derive the one symbol-day an atomic commit
//! protects and hold its exclusive lease across the commit.

use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

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
    acquisition_timeout: Duration,
}

impl LeasedAtomicIngestCommitter {
    /// Build the committer over an injected inner committer and lease source.
    pub fn new(committer: Arc<dyn AtomicIngestCommitter>, leases: Arc<dyn LeaseProvider>) -> Self {
        Self {
            committer,
            leases,
            acquisition_timeout: Duration::from_secs(75),
        }
    }

    /// Bound waiting for an earlier symbol-day publication, before any mutation.
    ///
    /// The default exceeds Azure's longest finite lease (60 seconds), so a dead
    /// publisher can expire. Shorter operational deadlines may be selected by
    /// callers; zero selects immediate acquisition without waiting.
    pub fn with_acquisition_timeout(mut self, timeout: Duration) -> Self {
        self.acquisition_timeout = timeout;
        self
    }

    fn acquire(&self, scope: &SymbolDayScope) -> Result<Box<dyn lake_storage::Lease>, DataError> {
        let started = Instant::now();
        let mut delay = Duration::from_millis(10);
        loop {
            match self.leases.acquire(scope) {
                Ok(lease) => return Ok(lease),
                Err(DataError::Conflict(message)) => {
                    let elapsed = started.elapsed();
                    if elapsed >= self.acquisition_timeout {
                        return Err(DataError::Conflict(format!(
                            "publication lease acquisition timed out for {}: {message}",
                            scope.key()
                        )));
                    }
                    // Independent processes must not retry in lockstep. This
                    // jitter changes scheduling only; Instant owns the deadline.
                    let jitter = Duration::from_millis(
                        SystemTime::now()
                            .duration_since(UNIX_EPOCH)
                            .map_or(0, |now| u64::from(now.subsec_nanos()) % 11),
                    );
                    std::thread::sleep((delay + jitter).min(self.acquisition_timeout - elapsed));
                    delay = delay.saturating_mul(2).min(Duration::from_millis(250));
                }
                Err(error) => return Err(error),
            }
        }
    }
}

impl AtomicIngestCommitter for LeasedAtomicIngestCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        let scope = scope_for_coverage(coverage)?;
        let lease = self.acquire(&scope)?;
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
