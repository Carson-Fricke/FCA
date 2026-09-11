//! Provider-independent catalog records.
//!
//! Port of the `lake.catalog.models` value objects the security master,
//! listing-event deriver, and universe views consume. Only the records those
//! consumers need are ported.

use num::Decimal18;

use crate::error::DataError;
use crate::symbol::{canonical_osi_symbol, canonical_symbol};
use crate::time::Date;

/// How one entity came to be listed under a symbol.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ListingType {
    /// Initial public offering.
    Ipo,
    /// Direct listing.
    Direct,
    /// Corporate spin-off.
    Spinoff,
    /// Symbol rename that preserves the entity identity.
    Rename,
    /// Transfer from another venue.
    Transfer,
    /// Unknown.
    Unknown,
}

impl ListingType {
    /// The persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Ipo => "ipo",
            Self::Direct => "direct",
            Self::Spinoff => "spinoff",
            Self::Rename => "rename",
            Self::Transfer => "transfer",
            Self::Unknown => "unknown",
        }
    }
}

/// Why one listing episode ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum DelistingType {
    /// Renamed to a successor symbol.
    Rename,
    /// Merger.
    Merger,
    /// Acquisition.
    Acquisition,
    /// Bankruptcy.
    Bankruptcy,
    /// Voluntary delisting.
    Voluntary,
    /// Regulatory delisting.
    Regulatory,
    /// Unknown.
    Unknown,
}

impl DelistingType {
    /// The persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Rename => "rename",
            Self::Merger => "merger",
            Self::Acquisition => "acquisition",
            Self::Bankruptcy => "bankruptcy",
            Self::Voluntary => "voluntary",
            Self::Regulatory => "regulatory",
            Self::Unknown => "unknown",
        }
    }
}

/// The side of an option contract.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum OptionType {
    /// Call.
    Call,
    /// Put.
    Put,
}

impl OptionType {
    /// The persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Call => "call",
            Self::Put => "put",
        }
    }
}

/// Point-in-time listing episode for one entity under one symbol.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ListingRecord {
    /// Entity identity shared across a rename chain.
    pub entity_id: String,
    /// Canonical symbol.
    pub symbol: String,
    /// First session traded under `symbol` (inclusive).
    pub listed_date: Date,
    /// Last session traded under `symbol` (inclusive), if ended.
    pub delisted_date: Option<Date>,
    /// How the entity came to be listed.
    pub listing_type: ListingType,
    /// Why the episode ended, if ended.
    pub delisting_type: Option<DelistingType>,
    /// Final print once delisted, if known.
    pub final_price: Option<Decimal18>,
}

impl ListingRecord {
    /// Build and validate a listing episode.
    pub fn new(
        entity_id: &str,
        symbol: &str,
        listed_date: Date,
        delisted_date: Option<Date>,
        listing_type: ListingType,
        delisting_type: Option<DelistingType>,
        final_price: Option<Decimal18>,
    ) -> Result<Self, DataError> {
        let entity = entity_id.trim().to_uppercase();
        if entity.is_empty() {
            return Err(DataError::value("entity_id must not be empty"));
        }
        if let Some(delisted) = delisted_date {
            if listed_date > delisted {
                return Err(DataError::value(
                    "listed_date must be on or before delisted_date",
                ));
            }
        }
        if delisted_date.is_none() && delisting_type.is_some() {
            return Err(DataError::value("delisting_type requires a delisted_date"));
        }
        if delisted_date.is_some() && delisting_type.is_none() {
            return Err(DataError::value("delisted_date requires a delisting_type"));
        }
        if let Some(price) = final_price {
            if delisted_date.is_none() {
                return Err(DataError::value("final_price requires a delisted_date"));
            }
            if !price.is_positive() {
                return Err(DataError::value("final_price must be positive"));
            }
        }
        Ok(Self {
            entity_id: entity,
            symbol: canonical_symbol(symbol)?,
            listed_date,
            delisted_date,
            listing_type,
            delisting_type,
            final_price,
        })
    }

    /// Whether the listing itself was known on `as_of`.
    pub fn is_known(&self, as_of: Date) -> bool {
        self.listed_date <= as_of
    }

    /// Point-in-time activity of this episode on `day`.
    pub fn active_on(&self, day: Date, as_of: Date) -> bool {
        if self.listed_date > as_of || day < self.listed_date {
            return false;
        }
        match self.delisted_date {
            None => true,
            Some(delisted) if delisted > as_of => true,
            Some(delisted) => day <= delisted,
        }
    }
}

/// Point-in-time option contract metadata held by the contracts catalog.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContractRecord {
    /// Canonical compact OSI symbol.
    pub symbol: String,
    /// Canonical underlying root.
    pub underlying: String,
    /// Expiration date.
    pub expiration_date: Date,
    /// Positive strike price.
    pub strike_price: Decimal18,
    /// Call or put.
    pub option_type: OptionType,
    /// Contract multiplier.
    pub multiplier: u32,
    /// First listed date, if known.
    pub listed_date: Option<Date>,
    /// Delisted date, if known.
    pub delisted_date: Option<Date>,
}

impl ContractRecord {
    /// Build and validate a contract record.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        symbol: &str,
        underlying: &str,
        expiration_date: Date,
        strike_price: Decimal18,
        option_type: OptionType,
        multiplier: u32,
        listed_date: Option<Date>,
        delisted_date: Option<Date>,
    ) -> Result<Self, DataError> {
        if !strike_price.is_positive() {
            return Err(DataError::value("strike_price must be positive"));
        }
        if multiplier == 0 {
            return Err(DataError::value("multiplier must be positive"));
        }
        if let (Some(listed), Some(delisted)) = (listed_date, delisted_date) {
            if listed > delisted {
                return Err(DataError::value(
                    "listed_date must be on or before delisted_date",
                ));
            }
        }
        Ok(Self {
            symbol: canonical_osi_symbol(symbol)?,
            underlying: canonical_symbol(underlying)?,
            expiration_date,
            strike_price,
            option_type,
            multiplier,
            listed_date,
            delisted_date,
        })
    }

    /// Short alias used by option selectors.
    pub fn strike(&self) -> Decimal18 {
        self.strike_price
    }
}

/// A persisted daily front set for one underlying and selection date.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DailyFrontSet {
    /// Canonical underlying root.
    pub underlying: String,
    /// Selection date the set was persisted for.
    pub selection_date: Date,
    /// Contracts in the set.
    pub contracts: Vec<ContractRecord>,
}

/// Read boundary over the market-lake listings table.
pub trait ListingsCatalogPort {
    /// Return listing episodes filtered by entity, symbol, and knowledge.
    fn list(
        &self,
        entity_id: Option<&str>,
        symbol: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ListingRecord>, DataError>;
}

/// Read boundary over the market-lake contracts catalog.
pub trait ContractsCatalogPort {
    /// Return contract metadata by OSI symbol.
    fn get(&self, symbol: &str) -> Result<Option<ContractRecord>, DataError>;
    /// Return the persisted front set for one underlying and date.
    fn get_front_set(
        &self,
        underlying: &str,
        selection_date: Date,
    ) -> Result<Option<DailyFrontSet>, DataError>;
}

/// A trivial in-memory listings catalog for tests and downstream fixtures.
#[derive(Clone, Debug, Default)]
pub struct MemoryListingsCatalog {
    /// Retained episodes.
    pub episodes: Vec<ListingRecord>,
}

impl ListingsCatalogPort for MemoryListingsCatalog {
    fn list(
        &self,
        entity_id: Option<&str>,
        symbol: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ListingRecord>, DataError> {
        Ok(self
            .episodes
            .iter()
            .filter(|episode| {
                entity_id.is_none_or(|entity| episode.entity_id == entity.trim().to_uppercase())
            })
            .filter(|episode| {
                symbol.is_none_or(|value| {
                    canonical_symbol(value)
                        .map(|canonical| canonical == episode.symbol)
                        .unwrap_or(false)
                })
            })
            .filter(|episode| as_of.is_none_or(|date| episode.listed_date <= date))
            .cloned()
            .collect())
    }
}

/// A trivial in-memory contracts catalog for tests and downstream fixtures.
#[derive(Clone, Debug, Default)]
pub struct MemoryContractsCatalog {
    /// Retained contracts, keyed by OSI symbol.
    pub contracts: Vec<ContractRecord>,
    /// Retained front sets.
    pub front_sets: Vec<DailyFrontSet>,
}

impl ContractsCatalogPort for MemoryContractsCatalog {
    fn get(&self, symbol: &str) -> Result<Option<ContractRecord>, DataError> {
        let target = canonical_osi_symbol(symbol)?;
        Ok(self
            .contracts
            .iter()
            .find(|record| record.symbol == target)
            .cloned())
    }

    fn get_front_set(
        &self,
        underlying: &str,
        selection_date: Date,
    ) -> Result<Option<DailyFrontSet>, DataError> {
        let target = canonical_symbol(underlying)?;
        Ok(self
            .front_sets
            .iter()
            .find(|set| set.underlying == target && set.selection_date == selection_date)
            .cloned())
    }
}

#[cfg(test)]
#[path = "../tests/unit/catalog.rs"]
mod tests;
