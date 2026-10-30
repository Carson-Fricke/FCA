//! Re-iterable, bounded chronological replay over pinned market-data snapshots.
//!
//! At most one decoded batch and one same-time group per input are retained.
//! Dropping the consumer closes its rendezvous channels and stops producers.

use super::fixture::DataPreflight;
use super::fixture::ExchangeSession;
use super::fixture::MarketEvent;
use harness_trading::replay_errors::BacktestError;
use std::collections::BTreeSet;
use std::sync::Arc;

pub type Events = Box<dyn Iterator<Item = Result<MarketEvent, BacktestError>> + Send>;
pub type Factory = Arc<dyn Fn() -> Result<Events, BacktestError> + Send + Sync>;
pub type PlannedReplay = (Arc<StreamedReplay>, Vec<MarketEvent>, Vec<ExchangeSession>);

/// Immutable replay factory and evidence collected without retaining tick rows.
pub struct StreamedReplay {
    factory: Factory,
    pub evidence: DataPreflight,
    pub reporter: Option<harness_contracts::progress::ProgressReporter>,
    pub(crate) coverage: BTreeSet<(String, String)>,
    pub expected_count: Option<usize>,
}
impl std::fmt::Debug for StreamedReplay {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StreamedReplay")
            .field("evidence", &self.evidence)
            .finish()
    }
}
impl PartialEq for StreamedReplay {
    fn eq(&self, other: &Self) -> bool {
        self.evidence == other.evidence
    }
}
impl Eq for StreamedReplay {}
impl StreamedReplay {
    pub fn open(&self) -> Result<Events, BacktestError> {
        (self.factory)()
    }
    pub fn covers(&self, kind: &str, symbol: &str) -> bool {
        self.coverage.contains(&(kind.into(), symbol.into()))
    }

    /// Build a bounded replay from already-established metadata, without opening ticks.
    pub fn new(
        factory: Factory,
        evidence: DataPreflight,
        coverage: BTreeSet<(String, String)>,
        expected_count: Option<usize>,
        reporter: Option<harness_contracts::progress::ProgressReporter>,
    ) -> Self {
        Self {
            factory,
            evidence,
            coverage,
            expected_count,
            reporter,
        }
    }
}

/// Total causal ordering used by in-memory and streamed inputs.
pub fn key(event: &MarketEvent) -> (i64, u64, u8) {
    (event.timestamp_ns(), event.sequence(), event.kind() as u8)
}
