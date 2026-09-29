//! Persistence ports for catalog capabilities.
//!
//! The traits are object-safe, `Send + Sync`, and return [`DataError`] so a
//! memory adapter, the local JSON store, and future hosted adapters are
//! interchangeable.

use lake_contracts::{AssetClass, DataError, DataType, Date};

use crate::front_sets::DailyFrontSet;
use crate::models::{
    ContractRecord, CoverageRecord, JobRecord, JobState, ListingRecord, RunRecord, RunState,
};

/// Filters for coverage catalog queries.
///
/// The symbol filter is canonicalised before use and a reversed date range
/// (`start > end`) is a `ValueError`. An `interval` filter keeps only rows
/// stored under that canonical bars token.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CoverageQuery {
    /// Keep only this asset class.
    pub asset_class: Option<AssetClass>,
    /// Keep only this data type.
    pub data_type: Option<DataType>,
    /// Keep only this canonical symbol.
    pub symbol: Option<String>,
    /// Keep only rows stored under this canonical bars interval token.
    pub interval: Option<String>,
    /// Inclusive first session date.
    pub start: Option<Date>,
    /// Inclusive last session date.
    pub end: Option<Date>,
}

/// Persistence contract for symbol-day coverage metadata.
pub trait CatalogRepository: Send + Sync {
    /// Return one coverage row by logical key.
    ///
    /// The interval is part of the logical key: bars rows are stored under
    /// their canonical token and every other data type under `None`.
    fn get(
        &self,
        asset_class: AssetClass,
        data_type: DataType,
        interval: Option<&str>,
        symbol: &str,
        session_date: Date,
    ) -> Result<Option<CoverageRecord>, DataError>;

    /// Replace the row stored under the record's logical key.
    fn upsert(&self, record: &CoverageRecord) -> Result<(), DataError>;

    /// Return matching rows sorted by asset class, data type, symbol, date.
    fn list(&self, query: &CoverageQuery) -> Result<Vec<CoverageRecord>, DataError>;
}

/// Persistence contract for asynchronous job state.
pub trait JobRepository: Send + Sync {
    /// Return one job by identifier.
    fn get(&self, job_id: &str) -> Result<Option<JobRecord>, DataError>;

    /// Replace the job stored under the record's identifier.
    fn upsert(&self, record: &JobRecord) -> Result<(), DataError>;

    /// Return matching jobs sorted by job identifier.
    fn list(&self, state: Option<JobState>) -> Result<Vec<JobRecord>, DataError>;
}

/// Persistence contract for stored backtest run metadata.
pub trait RunRepository: Send + Sync {
    /// Return one run by identifier.
    fn get(&self, run_id: &str) -> Result<Option<RunRecord>, DataError>;

    /// Replace the run stored under the record's identifier.
    fn upsert(&self, record: &RunRecord) -> Result<(), DataError>;

    /// Return matching runs sorted by run identifier.
    fn list(
        &self,
        status: Option<RunState>,
        strategy_id: Option<&str>,
    ) -> Result<Vec<RunRecord>, DataError>;

    /// Remove a run, reporting whether it existed.
    fn delete(&self, run_id: &str) -> Result<bool, DataError>;
}

/// Persistence contract for option contract metadata.
pub trait ContractRepository: Send + Sync {
    /// Return one contract by canonical symbol.
    fn get(&self, symbol: &str) -> Result<Option<ContractRecord>, DataError>;

    /// Replace the contract stored under the record's symbol.
    fn upsert(&self, record: &ContractRecord) -> Result<(), DataError>;

    /// Return matching contracts sorted by symbol.
    ///
    /// With `as_of`, a contract is active when
    /// `(listed.is_none() || as_of >= listed) && (delisted.is_none() || as_of <= delisted)`.
    fn list(
        &self,
        underlying: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ContractRecord>, DataError>;
}

/// Persistence contract for point-in-time security listing episodes.
pub trait ListingRepository: Send + Sync {
    /// Return one episode by entity and symbol.
    fn get(&self, entity_id: &str, symbol: &str) -> Result<Option<ListingRecord>, DataError>;

    /// Replace the episode stored under the record's logical key.
    fn upsert(&self, record: &ListingRecord) -> Result<(), DataError>;

    /// Return matching episodes sorted by entity and symbol.
    ///
    /// With `as_of`, only episodes known on that date are returned.
    fn list(
        &self,
        entity_id: Option<&str>,
        symbol: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ListingRecord>, DataError>;
}

/// Persistence contract for date-specific front-set membership.
pub trait DailyFrontSetRepository: Send + Sync {
    /// Return the selected front set for one underlying and date.
    fn get(
        &self,
        underlying: &str,
        selection_date: Date,
    ) -> Result<Option<DailyFrontSet>, DataError>;

    /// Replace the membership for the front set's logical key.
    fn upsert(&self, front_set: &DailyFrontSet) -> Result<(), DataError>;

    /// Return matching front sets sorted by underlying and selection date.
    fn list(
        &self,
        underlying: Option<&str>,
        start: Option<Date>,
        end: Option<Date>,
    ) -> Result<Vec<DailyFrontSet>, DataError>;
}
