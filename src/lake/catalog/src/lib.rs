//! Coverage, job, run, contract, listing, and front-set records with their repositories.
//!
//! Coverage uses one fenced symbol-day manifest over local/Azure object ports.
//! Other metadata retains in-memory and Azure Table adapters. No historical
//! coverage store or runtime conversion is supported.

mod azure;
mod front_sets;
mod memory;
mod models;
mod ports;
mod publications;

pub use azure::{
    contract_from_entity, contract_to_entity, job_from_entity, job_to_entity, listing_from_entity,
    listing_partition_for, listing_partition_key, listing_to_entity, run_from_entity,
    run_to_entity, AzureTableContractRepository, AzureTableJobRepository,
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
    table_key, BarSource, ContractRecord, CoverageRecord, CoverageStatus, DelistingType,
    JobProgress, JobRecord, JobState, ListingRecord, ListingType, OptionType, RunRecord, RunState,
};
pub use ports::{
    CatalogRepository, ContractRepository, CoverageQuery, DailyFrontSetRepository, JobRepository,
    ListingRepository, RunRepository,
};
pub use publications::{scope_for_coverage, PublicationCatalog, PUBLICATION_PREFIX};
