//! Coverage, job, run, contract, listing, and front-set records with their repositories.
//!
//! This crate is the Rust port of `lake/catalog`: provider-independent value
//! objects, object-safe persistence ports, deterministic in-memory adapters,
//! and the indexed local JSON catalog store that reads and rewrites the
//! existing `catalog.json` byte-for-byte.

mod front_sets;
mod memory;
mod models;
mod ports;
mod store;

pub use front_sets::{
    ContractsCatalog, DailyFrontSet, FrontSetRecord, MemoryDailyFrontSetRepository,
};
pub use memory::{
    MemoryCatalogRepository, MemoryContractRepository, MemoryJobRepository,
    MemoryListingRepository, MemoryRunRepository,
};
pub use models::{
    table_key, ContractRecord, CoverageRecord, CoverageStatus, DelistingType, JobRecord, JobState,
    ListingRecord, ListingType, OptionType, RunRecord, RunState,
};
pub use ports::{
    CatalogRepository, ContractRepository, CoverageQuery, DailyFrontSetRepository, JobRepository,
    ListingRepository, RunRepository,
};
pub use store::JsonCatalogStore;
