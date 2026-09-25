//! Daily option front-set records and deterministic in-memory storage.
//!
//! Contract metadata remains owned by this crate's models. This module stores
//! the date-specific membership selected from that metadata so a later query
//! can reproduce exactly which OSI symbols were used for a session.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use lake_contracts::{DataError, Date};
use serde::de::{self, Deserializer};
use serde::ser::{SerializeStruct, Serializer};
use serde::{Deserialize, Serialize};

use crate::memory::guard;
use crate::models::{canonical_underlying, ContractRecord};
use crate::ports::{ContractRepository, DailyFrontSetRepository};

/// The immutable OSI membership selected for one underlying and date.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DailyFrontSet {
    /// Canonical uppercase underlying symbol.
    pub underlying: String,
    /// Session the membership applies to.
    pub selection_date: Date,
    /// Selected contracts sorted by expiration, strike, side, and symbol.
    pub contracts: Vec<ContractRecord>,
}

impl DailyFrontSet {
    /// Build a validated front set.
    ///
    /// Every contract must share the selected underlying, no symbol may
    /// repeat, and the contracts are stored sorted by
    /// `(expiration_date, strike_price, option_type, symbol)`. Mirrors
    /// Python's `DailyFrontSet.__post_init__`.
    pub fn new(
        underlying: impl Into<String>,
        selection_date: Date,
        contracts: Vec<ContractRecord>,
    ) -> Result<Self, DataError> {
        let underlying = canonical_underlying(&underlying.into())?;
        if contracts
            .iter()
            .any(|contract| contract.underlying != underlying)
        {
            return Err(DataError::value(
                "front-set contracts must share the selected underlying",
            ));
        }
        {
            let mut symbols = BTreeSet::new();
            for contract in &contracts {
                if !symbols.insert(contract.symbol.as_str()) {
                    return Err(DataError::value(
                        "front-set contracts must not contain duplicate symbols",
                    ));
                }
            }
        }
        let mut contracts = contracts;
        contracts.sort_by(contract_order);
        Ok(Self {
            underlying,
            selection_date,
            contracts,
        })
    }

    /// The stable logical key for this daily front set.
    pub fn key(&self) -> (String, Date) {
        (self.underlying.clone(), self.selection_date)
    }

    /// Selected OSI symbols in deterministic order.
    pub fn symbols(&self) -> Vec<String> {
        self.contracts
            .iter()
            .map(|contract| contract.symbol.clone())
            .collect()
    }

    /// Selected expiration dates in ascending order, without duplicates.
    pub fn expiration_dates(&self) -> Vec<Date> {
        self.contracts
            .iter()
            .map(|contract| contract.expiration_date)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

fn contract_order(left: &ContractRecord, right: &ContractRecord) -> std::cmp::Ordering {
    (
        left.expiration_date,
        left.strike_price,
        left.option_type.value(),
        left.symbol.as_str(),
    )
        .cmp(&(
            right.expiration_date,
            right.strike_price,
            right.option_type.value(),
            right.symbol.as_str(),
        ))
}

#[derive(Deserialize)]
struct FrontSetJson {
    underlying: String,
    #[serde(with = "crate::models::date_format")]
    selection_date: Date,
    contracts: Vec<ContractRecord>,
}

impl Serialize for DailyFrontSet {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("DailyFrontSet", 3)?;
        state.serialize_field("contracts", &self.contracts)?;
        state.serialize_field("selection_date", &self.selection_date.isoformat())?;
        state.serialize_field("underlying", &self.underlying)?;
        state.end()
    }
}

impl<'de> Deserialize<'de> for DailyFrontSet {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = FrontSetJson::deserialize(deserializer)?;
        DailyFrontSet::new(raw.underlying, raw.selection_date, raw.contracts)
            .map_err(de::Error::custom)
    }
}

/// Deterministic process-local storage for daily front sets.
#[derive(Debug, Default)]
pub struct MemoryDailyFrontSetRepository {
    front_sets: Mutex<BTreeMap<(String, Date), DailyFrontSet>>,
}

impl MemoryDailyFrontSetRepository {
    /// An empty repository.
    pub fn new() -> Self {
        Self::default()
    }

    /// A repository seeded with front sets, last duplicate key winning.
    pub fn from_records(front_sets: impl IntoIterator<Item = DailyFrontSet>) -> Self {
        let mut index = BTreeMap::new();
        for front_set in front_sets {
            index.insert(front_set.key(), front_set);
        }
        Self {
            front_sets: Mutex::new(index),
        }
    }
}

impl DailyFrontSetRepository for MemoryDailyFrontSetRepository {
    fn get(
        &self,
        underlying: &str,
        selection_date: Date,
    ) -> Result<Option<DailyFrontSet>, DataError> {
        let underlying = canonical_underlying(underlying)?;
        Ok(guard(&self.front_sets)
            .get(&(underlying, selection_date))
            .cloned())
    }

    fn upsert(&self, front_set: &DailyFrontSet) -> Result<(), DataError> {
        guard(&self.front_sets).insert(front_set.key(), front_set.clone());
        Ok(())
    }

    fn list(
        &self,
        underlying: Option<&str>,
        start: Option<Date>,
        end: Option<Date>,
    ) -> Result<Vec<DailyFrontSet>, DataError> {
        if let (Some(start), Some(end)) = (start, end) {
            if start > end {
                return Err(DataError::value("start must be on or before end"));
            }
        }
        let underlying = underlying.map(canonical_underlying).transpose()?;
        let front_sets = guard(&self.front_sets);
        let mut matches: Vec<DailyFrontSet> = front_sets
            .values()
            .filter(|front_set| {
                underlying
                    .as_deref()
                    .is_none_or(|value| value == front_set.underlying.as_str())
                    && start.is_none_or(|start| front_set.selection_date >= start)
                    && end.is_none_or(|end| front_set.selection_date <= end)
            })
            .cloned()
            .collect();
        matches.sort_by_key(DailyFrontSet::key);
        Ok(matches)
    }
}

/// A front set stored under an Azure Table-compatible record name.
pub type FrontSetRecord = DailyFrontSet;

/// Expose contract metadata and daily front-set membership together.
pub struct ContractsCatalog {
    /// Contract metadata repository.
    pub contracts: Arc<dyn ContractRepository>,
    /// Daily front-set repository.
    pub front_sets: Arc<dyn DailyFrontSetRepository>,
}

impl ContractsCatalog {
    /// Compose a catalog from both repositories.
    pub fn new(
        contracts: Arc<dyn ContractRepository>,
        front_sets: Arc<dyn DailyFrontSetRepository>,
    ) -> Self {
        Self {
            contracts,
            front_sets,
        }
    }

    /// Compose a catalog with an empty in-memory front-set repository.
    pub fn with_contracts(contracts: Arc<dyn ContractRepository>) -> Self {
        Self::new(contracts, Arc::new(MemoryDailyFrontSetRepository::new()))
    }

    /// Return contract metadata by OSI symbol.
    pub fn get(&self, symbol: &str) -> Result<Option<ContractRecord>, DataError> {
        self.contracts.get(symbol)
    }

    /// Persist contract metadata through the contract repository contract.
    pub fn upsert(&self, record: &ContractRecord) -> Result<(), DataError> {
        self.contracts.upsert(record)
    }

    /// Return active contract metadata for an underlying and date.
    pub fn list(
        &self,
        underlying: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ContractRecord>, DataError> {
        self.contracts.list(underlying, as_of)
    }

    /// Return the persisted front set for an underlying and date.
    pub fn get_front_set(
        &self,
        underlying: &str,
        selection_date: Date,
    ) -> Result<Option<DailyFrontSet>, DataError> {
        self.front_sets.get(underlying, selection_date)
    }

    /// Replace the persisted membership for one underlying and date.
    pub fn upsert_front_set(&self, front_set: &DailyFrontSet) -> Result<(), DataError> {
        self.front_sets.upsert(front_set)
    }

    /// Query persisted front sets by underlying and inclusive date range.
    pub fn list_front_sets(
        &self,
        underlying: Option<&str>,
        start: Option<Date>,
        end: Option<Date>,
    ) -> Result<Vec<DailyFrontSet>, DataError> {
        self.front_sets.list(underlying, start, end)
    }
}

#[cfg(test)]
#[path = "../tests/unit/front_sets.rs"]
mod tests;
