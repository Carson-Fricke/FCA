//! Equity and crypto ingest transactions, record validation, and the committer boundary.
//!
//! This crate is the Rust port of `lake.ingestion.common`,
//! `lake.ingestion.equities.transaction`, and
//! `lake.ingestion.crypto.transaction`. One transaction fetches every
//! requested data type concurrently through an injected provider boundary,
//! validates and deterministically sorts the provider records, stages one
//! Parquet object per data type, and publishes the objects together with their
//! coverage rows through exactly one caller-supplied atomic commit. Options
//! ingestion is deferred and lives outside this crate.
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

mod common;
mod crypto;
mod equities;
mod providers;
mod staging;

pub use common::{AtomicIngestCommitter, PartitionWriter, StagedParquetObject, TransientRetry};
pub use crypto::{CryptoClock, CryptoIngestResult, CryptoIngestTransaction};
pub use equities::{EquityIngestResult, EquityIngestTransaction};
pub use providers::{CryptoDataProvider, EquityDataProvider};
pub use staging::StagingPartitionWriter;
