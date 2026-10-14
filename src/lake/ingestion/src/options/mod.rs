//! Option ingestion: front-set selection, symbol-day transactions, backfill
//! planning, and the queue handler the future queue crate will compose.
//!
//! Ports `lake.ingestion.options`. Option work is deliberately kept at the OSI
//! symbol-day boundary: the option client can batch provider requests
//! internally, while each selected contract gets deterministic trade and quote
//! objects plus matching coverage rows. Like the equity and crypto ports, a
//! fetch, validation, or write failure publishes error coverage and no object,
//! never a partial success.
//!
//! Composition parity with Python: the local launcher rejects options and the
//! hosted runtime is equity-only, so option ingest is reached through the
//! queue handler and later CLI paths — never through the ensure wiring.

pub mod backfill;
pub mod resolve;
pub mod selector;
pub mod transaction;
pub mod worker;

pub use backfill::{
    enqueue_option_backfill, plan_option_backfill, OptionBackfillItem, OptionBackfillPlan,
    OptionBackfillRequest, OptionClock, OptionSpotProvider, QueueSender, SpotValues,
};
pub use selector::OptionChainSelector;
pub use transaction::{OptionIngestResult, OptionIngestTransaction, OptionMarketDataClient};
pub use worker::OptionQueueHandler;
