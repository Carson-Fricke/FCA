//! Equity, option, and crypto ingest transactions, record validation, and the
//! committer boundary.
//!
//! This crate is the Rust port of `lake.ingestion.common`,
//! `lake.ingestion.equities.transaction`,
//! `lake.ingestion.crypto.transaction`, and `lake.ingestion.options`. One
//! transaction fetches every requested data type concurrently through an
//! injected provider boundary, validates and deterministically sorts the
//! provider records, stages one Parquet object per data type, and publishes the
//! objects together with their coverage rows through exactly one
//! caller-supplied atomic commit. Option transactions work at the OSI
//! symbol-day boundary and are composed by the finite ACI worker.
//!
//! Two semantics are load-bearing and reproduced exactly:
//!
//! 1. **Failure atomicity** — a fetch, validation, or write failure publishes
//!    error coverage and no object, never a partial success; the committer is
//!    called exactly once per run with all staged objects drained.
//! 2. **Inclusive session bounds** — the fetch window and record validation
//!    both include the session close (or the partial-day clock bound), because
//!    Alpaca's `end` parameter is inclusive and the closing auction print
//!    belongs to its session.

mod bars;
mod common;
mod crypto;
mod ensure;
mod equities;
mod leased;
pub mod options;
mod providers;
mod publication;
mod staging;
mod trade_inputs;
pub use trade_inputs::{CryptoTradeRecord, EquityTradeRecord};

pub use bars::{
    derive_crypto_bars, derive_equity_bars, BarsIngestResult, BarsIngestTransaction,
    CatalogTradeReader, StoredTrade, StoredTradePartition, TradePartitionReader,
};
pub use common::{AtomicIngestCommitter, PartitionWriter, StagedParquetObject, TransientRetry};
pub use crypto::{CryptoClock, CryptoIngestResult, CryptoIngestTransaction};
pub use ensure::{EnsureIngest, EnsureProgress, EnsureProviders};
pub use equities::{EquityIngestResult, EquityIngestTransaction};
pub use leased::{scope_for_coverage, LeasedAtomicIngestCommitter};
pub use options::{
    plan_option_backfill, OptionBackfillItem, OptionBackfillPlan, OptionBackfillRequest,
    OptionChainSelector, OptionClock, OptionIngestResult, OptionIngestTransaction,
    OptionMarketDataClient, OptionSpotProvider, SpotValues,
};
pub use providers::{BarsDataProvider, CryptoDataProvider, EquityDataProvider};
pub use publication::ObjectCatalogCommitter;
pub use staging::StagingPartitionWriter;
