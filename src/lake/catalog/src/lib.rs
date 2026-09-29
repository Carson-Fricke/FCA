//! Coverage, job, run, contract, listing, and front-set records with their repositories.
//!
//! This crate is the Rust port of `lake/catalog`: provider-independent value
//! objects, object-safe persistence ports, deterministic in-memory adapters,
//! the indexed local JSON catalog store that reads and rewrites the
//! existing `catalog.json` byte-for-byte, and the Azure Table adapters.

mod azure;
mod front_sets;
mod memory;
mod models;
mod ports;
mod store;

pub use azure::{
    contract_from_entity, contract_to_entity, coverage_from_entity, coverage_partition_key,
    coverage_to_entity, job_from_entity, job_to_entity, listing_from_entity, listing_partition_for,
    listing_partition_key, listing_to_entity, run_from_entity, run_to_entity,
    AzureTableCatalogRepository, AzureTableContractRepository, AzureTableJobRepository,
    AzureTableListingRepository, AzureTableRunRepository, SdkTableEntityStore, TableEntity,
    TableEntityStore, TableQuery,
};

pub use front_sets::{
    ContractsCatalog, DailyFrontSet, FrontSetRecord, MemoryDailyFrontSetRepository,
};
pub use memory::{
    MemoryCatalogRepository, MemoryContractRepository, MemoryJobRepository,
    MemoryListingRepository, MemoryRunRepository,
};
pub use models::{
    table_key, BarSource, ContractRecord, CoverageRecord, CoverageStatus, DelistingType, JobRecord,
    JobState, ListingRecord, ListingType, OptionType, RunRecord, RunState,
};
pub use ports::{
    CatalogRepository, ContractRepository, CoverageQuery, DailyFrontSetRepository, JobRepository,
    ListingRepository, RunRepository,
};
pub use store::JsonCatalogStore;
