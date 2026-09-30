//! Deterministic in-memory catalog adapters.
//!
//! Every repository is a `Mutex`-guarded `BTreeMap` keyed by each record's
//! logical key, mirrors the Python memory adapters' filters and sort orders,
//! and is safe to share across threads.

use std::collections::BTreeMap;
use std::sync::{Mutex, MutexGuard};

use lake_contracts::{canonical_symbol, AssetClass, DataError, DataType, Date};

use crate::models::{
    canonical_entity_id, ContractRecord, CoverageRecord, JobRecord, JobState, ListingRecord,
    RunRecord, RunState,
};
use crate::ports::{
    CatalogRepository, ContractRepository, CoverageQuery, JobRepository, ListingRepository,
    RunRepository,
};

/// Lock a repository mutex, ignoring poisoning: the guarded maps are plain
/// data and a panic elsewhere must not take the repository down with it.
pub(crate) fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// The logical key every coverage record is indexed by.
type CoverageKey = (AssetClass, DataType, String, Option<String>, Date);

/// Deterministic process-local coverage storage.
#[derive(Debug, Default)]
pub struct MemoryCatalogRepository {
    records: Mutex<BTreeMap<CoverageKey, CoverageRecord>>,
}

impl MemoryCatalogRepository {
    /// An empty repository.
    pub fn new() -> Self {
        Self::default()
    }

    /// A repository seeded with records, last duplicate key winning.
    pub fn from_records(records: impl IntoIterator<Item = CoverageRecord>) -> Self {
        let mut index = BTreeMap::new();
        for record in records {
            index.insert(record.key(), record);
        }
        Self {
            records: Mutex::new(index),
        }
    }
}

impl CatalogRepository for MemoryCatalogRepository {
    fn get(
        &self,
        asset_class: AssetClass,
        data_type: DataType,
        interval: Option<&str>,
        symbol: &str,
        session_date: Date,
    ) -> Result<Option<CoverageRecord>, DataError> {
        let symbol = canonical_symbol(symbol)?;
        Ok(guard(&self.records)
            .get(&(
                asset_class,
                data_type,
                symbol,
                interval.map(str::to_string),
                session_date,
            ))
            .cloned())
    }

    fn upsert(&self, record: &CoverageRecord) -> Result<(), DataError> {
        guard(&self.records).insert(record.key(), record.clone());
        Ok(())
    }

    fn list(&self, query: &CoverageQuery) -> Result<Vec<CoverageRecord>, DataError> {
        let symbol = query.symbol.as_deref().map(canonical_symbol).transpose()?;
        if let (Some(start), Some(end)) = (query.start, query.end) {
            if start > end {
                return Err(DataError::value("start must be on or before end"));
            }
        }
        let records = guard(&self.records);
        let mut matches: Vec<CoverageRecord> = records
            .values()
            .filter(|record| {
                query
                    .asset_class
                    .is_none_or(|value| value == record.asset_class)
                    && query
                        .data_type
                        .is_none_or(|value| value == record.data_type)
                    && symbol
                        .as_deref()
                        .is_none_or(|value| value == record.symbol.as_str())
                    && query
                        .interval
                        .as_deref()
                        .is_none_or(|value| record.interval.as_deref() == Some(value))
                    && query.start.is_none_or(|start| record.session_date >= start)
                    && query.end.is_none_or(|end| record.session_date <= end)
            })
            .cloned()
            .collect();
        matches.sort_by(|left, right| left.sort_key().cmp(&right.sort_key()));
        Ok(matches)
    }
}

/// Deterministic process-local job storage.
#[derive(Debug, Default)]
pub struct MemoryJobRepository {
    records: Mutex<BTreeMap<String, JobRecord>>,
}

impl MemoryJobRepository {
    /// An empty repository.
    pub fn new() -> Self {
        Self::default()
    }

    /// A repository seeded with records, last duplicate identifier winning.
    pub fn from_records(records: impl IntoIterator<Item = JobRecord>) -> Self {
        let mut index = BTreeMap::new();
        for record in records {
            index.insert(record.job_id.clone(), record);
        }
        Self {
            records: Mutex::new(index),
        }
    }
}

impl JobRepository for MemoryJobRepository {
    fn get(&self, job_id: &str) -> Result<Option<JobRecord>, DataError> {
        Ok(guard(&self.records).get(job_id.trim()).cloned())
    }

    fn upsert(&self, record: &JobRecord) -> Result<(), DataError> {
        guard(&self.records).insert(record.job_id.clone(), record.clone());
        Ok(())
    }

    fn list(&self, state: Option<JobState>) -> Result<Vec<JobRecord>, DataError> {
        let records = guard(&self.records);
        let mut matches: Vec<JobRecord> = records
            .values()
            .filter(|record| state.is_none_or(|value| value == record.state))
            .cloned()
            .collect();
        matches.sort_by(|left, right| left.job_id.cmp(&right.job_id));
        Ok(matches)
    }
}

/// Deterministic process-local run storage.
#[derive(Debug, Default)]
pub struct MemoryRunRepository {
    records: Mutex<BTreeMap<String, RunRecord>>,
}

impl MemoryRunRepository {
    /// An empty repository.
    pub fn new() -> Self {
        Self::default()
    }

    /// A repository seeded with records, last duplicate identifier winning.
    pub fn from_records(records: impl IntoIterator<Item = RunRecord>) -> Self {
        let mut index = BTreeMap::new();
        for record in records {
            index.insert(record.run_id.clone(), record);
        }
        Self {
            records: Mutex::new(index),
        }
    }
}

impl RunRepository for MemoryRunRepository {
    fn get(&self, run_id: &str) -> Result<Option<RunRecord>, DataError> {
        Ok(guard(&self.records).get(run_id.trim()).cloned())
    }

    fn upsert(&self, record: &RunRecord) -> Result<(), DataError> {
        guard(&self.records).insert(record.run_id.clone(), record.clone());
        Ok(())
    }

    fn list(
        &self,
        status: Option<RunState>,
        strategy_id: Option<&str>,
    ) -> Result<Vec<RunRecord>, DataError> {
        let strategy_id = strategy_id.map(str::trim);
        let records = guard(&self.records);
        let mut matches: Vec<RunRecord> = records
            .values()
            .filter(|record| {
                status.is_none_or(|value| value == record.status)
                    && strategy_id.is_none_or(|value| value == record.strategy_id.as_str())
            })
            .cloned()
            .collect();
        matches.sort_by(|left, right| left.run_id.cmp(&right.run_id));
        Ok(matches)
    }

    fn delete(&self, run_id: &str) -> Result<bool, DataError> {
        Ok(guard(&self.records).remove(run_id.trim()).is_some())
    }
}

/// Deterministic process-local contract storage.
#[derive(Debug, Default)]
pub struct MemoryContractRepository {
    records: Mutex<BTreeMap<String, ContractRecord>>,
}

impl MemoryContractRepository {
    /// An empty repository.
    pub fn new() -> Self {
        Self::default()
    }

    /// A repository seeded with records, last duplicate symbol winning.
    pub fn from_records(records: impl IntoIterator<Item = ContractRecord>) -> Self {
        let mut index = BTreeMap::new();
        for record in records {
            index.insert(record.symbol.clone(), record);
        }
        Self {
            records: Mutex::new(index),
        }
    }
}

impl ContractRepository for MemoryContractRepository {
    fn get(&self, symbol: &str) -> Result<Option<ContractRecord>, DataError> {
        let symbol = canonical_symbol(symbol)?;
        Ok(guard(&self.records).get(&symbol).cloned())
    }

    fn upsert(&self, record: &ContractRecord) -> Result<(), DataError> {
        guard(&self.records).insert(record.symbol.clone(), record.clone());
        Ok(())
    }

    fn list(
        &self,
        underlying: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ContractRecord>, DataError> {
        let underlying = underlying.map(canonical_symbol).transpose()?;
        let records = guard(&self.records);
        let mut matches: Vec<ContractRecord> = records
            .values()
            .filter(|record| {
                underlying
                    .as_deref()
                    .is_none_or(|value| value == record.underlying.as_str())
                    && as_of.is_none_or(|day| contract_is_active(record, day))
            })
            .cloned()
            .collect();
        matches.sort_by(|left, right| left.symbol.cmp(&right.symbol));
        Ok(matches)
    }
}

fn contract_is_active(record: &ContractRecord, as_of: Date) -> bool {
    record.listed_date.is_none_or(|listed| as_of >= listed)
        && record
            .delisted_date
            .is_none_or(|delisted| as_of <= delisted)
}

/// Deterministic process-local listing storage.
#[derive(Debug, Default)]
pub struct MemoryListingRepository {
    records: Mutex<BTreeMap<(String, String), ListingRecord>>,
}

impl MemoryListingRepository {
    /// An empty repository.
    pub fn new() -> Self {
        Self::default()
    }

    /// A repository seeded with records, last duplicate key winning.
    pub fn from_records(records: impl IntoIterator<Item = ListingRecord>) -> Self {
        let mut index = BTreeMap::new();
        for record in records {
            index.insert(record.key(), record);
        }
        Self {
            records: Mutex::new(index),
        }
    }
}

impl ListingRepository for MemoryListingRepository {
    fn get(&self, entity_id: &str, symbol: &str) -> Result<Option<ListingRecord>, DataError> {
        let entity_id = canonical_entity_id(entity_id)?;
        let symbol = canonical_symbol(symbol)?;
        Ok(guard(&self.records).get(&(entity_id, symbol)).cloned())
    }

    fn upsert(&self, record: &ListingRecord) -> Result<(), DataError> {
        guard(&self.records).insert(record.key(), record.clone());
        Ok(())
    }

    fn list(
        &self,
        entity_id: Option<&str>,
        symbol: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ListingRecord>, DataError> {
        let entity_id = entity_id.map(canonical_entity_id).transpose()?;
        let symbol = symbol.map(canonical_symbol).transpose()?;
        let records = guard(&self.records);
        let mut matches: Vec<ListingRecord> = records
            .values()
            .filter(|record| {
                entity_id
                    .as_deref()
                    .is_none_or(|value| value == record.entity_id.as_str())
                    && symbol
                        .as_deref()
                        .is_none_or(|value| value == record.symbol.as_str())
                    && as_of.is_none_or(|day| record.is_known(day))
            })
            .cloned()
            .collect();
        matches.sort_by_key(ListingRecord::key);
        Ok(matches)
    }
}
