//! Option ingestion: front-set selection, symbol-day transactions, backfill
//! planning. Provider execution is composed only by the finite ACA worker.
//!
//! Ports `lake.ingestion.options`. Option work is deliberately kept at the OSI
//! symbol-day boundary: the option client can batch provider requests
//! internally, while each selected contract gets deterministic trade and quote
//! objects plus matching coverage rows. Like the equity and crypto ports, a
//! fetch, validation, or write failure publishes error coverage and no object,
//! never a partial success.
//!

pub mod backfill;
pub mod resolve;
pub mod selector;
pub mod transaction;

pub use backfill::{
    plan_option_backfill, OptionBackfillItem, OptionBackfillPlan, OptionBackfillRequest,
    OptionClock, OptionSpotProvider, SpotValues,
};
pub use selector::OptionChainSelector;
pub use transaction::{OptionIngestResult, OptionIngestTransaction, OptionMarketDataClient};
