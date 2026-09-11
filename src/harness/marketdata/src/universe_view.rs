//! Point-in-time strategy universe with hard look-ahead clipping.
//!
//! Port of `harness.data.universe_view`. The view composes point-in-time member
//! sources -- equity listings and option contract universes -- under a union or
//! intersection rule and never reads beyond the clock head.

use std::collections::HashSet;
use std::rc::Rc;

use crate::catalog::{ContractRecord, ContractsCatalogPort, ListingRecord, ListingsCatalogPort};
use crate::clock::Clock;
use crate::error::DataError;
use crate::symbol::{canonical_osi_symbol, canonical_symbol};
use crate::time::{Date, DateTime};

/// The asset kind a universe member's record describes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum MemberKind {
    /// Listed equity.
    Equity,
    /// Listed option.
    Option,
}

impl MemberKind {
    /// The persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Equity => "equity",
            Self::Option => "option",
        }
    }

    fn order(self) -> u8 {
        match self {
            Self::Equity => 0,
            Self::Option => 1,
        }
    }
}

/// How membership is composed across a view's member sources.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UniverseComposition {
    /// Union of all source membership.
    Union,
    /// Intersection of all source membership.
    Intersection,
}

/// One selectable universe symbol with its point-in-time record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum UniverseMember {
    /// An equity listing member.
    Equity {
        /// Canonical symbol.
        symbol: String,
        /// Point-in-time listing record.
        listing: ListingRecord,
    },
    /// An option contract member.
    Option {
        /// Canonical OSI symbol.
        symbol: String,
        /// Point-in-time contract record.
        contract: ContractRecord,
    },
}

impl UniverseMember {
    /// Build a validated equity member.
    pub fn equity(listing: ListingRecord) -> Self {
        Self::Equity {
            symbol: listing.symbol.clone(),
            listing,
        }
    }

    /// Build a validated option member.
    pub fn option(contract: ContractRecord) -> Self {
        Self::Option {
            symbol: contract.symbol.clone(),
            contract,
        }
    }

    /// The member symbol.
    pub fn symbol(&self) -> &str {
        match self {
            Self::Equity { symbol, .. } | Self::Option { symbol, .. } => symbol,
        }
    }

    /// The member kind.
    pub fn kind(&self) -> MemberKind {
        match self {
            Self::Equity { .. } => MemberKind::Equity,
            Self::Option { .. } => MemberKind::Option,
        }
    }

    /// The owning entity for equities, or the underlying for options.
    pub fn entity_id(&self) -> &str {
        match self {
            Self::Equity { listing, .. } => &listing.entity_id,
            Self::Option { contract, .. } => &contract.underlying,
        }
    }
}

fn contract_visible(record: &ContractRecord, underlying: &str, as_of: Date) -> bool {
    record.underlying == underlying
        && as_of <= record.expiration_date
        && record.listed_date.is_none_or(|listed| as_of >= listed)
        && record
            .delisted_date
            .is_none_or(|delisted| as_of <= delisted)
}

fn member_covers(member: &UniverseMember, as_of: Date) -> bool {
    match member {
        UniverseMember::Equity { listing, .. } => listing.active_on(as_of, as_of),
        UniverseMember::Option { contract, .. } => {
            contract_visible(contract, &contract.underlying, as_of)
        }
    }
}

/// Point-in-time member source read on explicit engine dates.
pub trait UniverseSource {
    /// The source's members valid on `as_of`, in sorted order.
    fn members(&self, as_of: Date) -> Result<Vec<UniverseMember>, DataError>;
}

/// Point-in-time equity membership from the market-lake listings table.
pub struct EquityListingUniverse {
    listings: Rc<dyn ListingsCatalogPort>,
}

impl EquityListingUniverse {
    /// Build a source reading only `listings`.
    pub fn new(listings: Rc<dyn ListingsCatalogPort>) -> Self {
        Self { listings }
    }
}

impl UniverseSource for EquityListingUniverse {
    fn members(&self, as_of: Date) -> Result<Vec<UniverseMember>, DataError> {
        let mut episodes = self.listings.list(None, None, None)?;
        episodes.sort_by(|a, b| (a.listed_date, &a.symbol).cmp(&(b.listed_date, &b.symbol)));
        let mut active: std::collections::HashMap<String, ListingRecord> =
            std::collections::HashMap::new();
        for episode in episodes {
            if episode.active_on(as_of, as_of) {
                active.insert(episode.entity_id.clone(), episode);
            }
        }
        let mut members: Vec<UniverseMember> =
            active.into_values().map(UniverseMember::equity).collect();
        members.sort_by(|a, b| a.symbol().cmp(b.symbol()));
        Ok(members)
    }
}

/// Point-in-time option membership from the market-lake contracts catalog.
pub struct OptionContractUniverse {
    catalog: Rc<dyn ContractsCatalogPort>,
    underlying: String,
    static_symbols: Option<Vec<String>>,
}

impl OptionContractUniverse {
    /// Build a source; `static_symbols` selects named OSI symbols.
    pub fn new(
        catalog: Rc<dyn ContractsCatalogPort>,
        underlying: &str,
        static_symbols: Option<Vec<String>>,
    ) -> Result<Self, DataError> {
        let resolved = match static_symbols {
            None => None,
            Some(symbols) => {
                let mut canonical = Vec::with_capacity(symbols.len());
                for symbol in symbols {
                    canonical.push(canonical_osi_symbol(&symbol)?);
                }
                if canonical.is_empty() {
                    return Err(DataError::value(
                        "static_symbols must name at least one OSI symbol when provided",
                    ));
                }
                canonical.sort();
                for window in canonical.windows(2) {
                    if window[0] == window[1] {
                        return Err(DataError::value(
                            "static_symbols must not contain duplicate OSI symbols",
                        ));
                    }
                }
                Some(canonical)
            }
        };
        Ok(Self {
            catalog,
            underlying: canonical_symbol(underlying)?,
            static_symbols: resolved,
        })
    }
}

impl UniverseSource for OptionContractUniverse {
    fn members(&self, as_of: Date) -> Result<Vec<UniverseMember>, DataError> {
        let records = match &self.static_symbols {
            None => self
                .catalog
                .get_front_set(&self.underlying, as_of)?
                .map(|set| set.contracts)
                .unwrap_or_default(),
            Some(symbols) => {
                let mut records = Vec::new();
                for symbol in symbols {
                    if let Some(record) = self.catalog.get(symbol)? {
                        if record.underlying == self.underlying {
                            records.push(record);
                        }
                    }
                }
                records
            }
        };
        let mut members: Vec<UniverseMember> = records
            .into_iter()
            .filter(|record| contract_visible(record, &self.underlying, as_of))
            .map(UniverseMember::option)
            .collect();
        members.sort_by(|a, b| a.symbol().cmp(b.symbol()));
        Ok(members)
    }
}

/// The universe members proven for one engine date.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UniverseSnapshot {
    /// Engine date.
    pub as_of: Date,
    /// Composition rule.
    pub composition: UniverseComposition,
    /// Visible members in `(kind, symbol)` order.
    pub members: Vec<UniverseMember>,
}

impl UniverseSnapshot {
    /// Build a snapshot, clipping and de-duplicating members.
    pub fn new(
        as_of: Date,
        composition: UniverseComposition,
        members: Vec<UniverseMember>,
    ) -> Self {
        let mut selected = Vec::new();
        let mut seen: HashSet<(MemberKind, String)> = HashSet::new();
        for member in members {
            if !member_covers(&member, as_of) {
                continue;
            }
            let key = (member.kind(), member.symbol().to_string());
            if seen.insert(key) {
                selected.push(member);
            }
        }
        selected
            .sort_by(|a, b| (a.kind().order(), a.symbol()).cmp(&(b.kind().order(), b.symbol())));
        Self {
            as_of,
            composition,
            members: selected,
        }
    }

    /// Member symbols in deterministic `(kind, symbol)` order.
    pub fn symbols(&self) -> Vec<String> {
        self.members
            .iter()
            .map(|member| member.symbol().to_string())
            .collect()
    }

    /// Equity member symbols in deterministic order.
    pub fn equities(&self) -> Vec<String> {
        self.members
            .iter()
            .filter(|member| member.kind() == MemberKind::Equity)
            .map(|member| member.symbol().to_string())
            .collect()
    }

    /// Option member symbols in deterministic order.
    pub fn options(&self) -> Vec<String> {
        self.members
            .iter()
            .filter(|member| member.kind() == MemberKind::Option)
            .map(|member| member.symbol().to_string())
            .collect()
    }

    /// Member counts by kind for run-manifest cleanliness records.
    pub fn provenance(&self) -> std::collections::BTreeMap<String, usize> {
        let mut counts = std::collections::BTreeMap::new();
        counts.insert("total".to_string(), self.members.len());
        for member in &self.members {
            *counts.entry(member.kind().value().to_string()).or_insert(0) += 1;
        }
        counts
    }

    /// Whether `symbol` belongs to this snapshot's universe.
    pub fn membership(&self, symbol: &str) -> bool {
        self.member(symbol).is_some()
    }

    /// One member with its point-in-time record, if selected.
    pub fn member(&self, symbol: &str) -> Option<&UniverseMember> {
        let target = canonical_symbol(symbol).ok()?;
        self.members.iter().find(|member| member.symbol() == target)
    }
}

/// Point-in-time strategy universe clipped to the engine clock.
pub struct UniverseView {
    clock: Box<dyn Clock>,
    sources: Vec<Rc<dyn UniverseSource>>,
    composition: UniverseComposition,
}

impl UniverseView {
    /// Build a view over at least one source.
    pub fn new(
        clock: Box<dyn Clock>,
        sources: Vec<Rc<dyn UniverseSource>>,
        composition: UniverseComposition,
    ) -> Result<Self, DataError> {
        if sources.is_empty() {
            return Err(DataError::value(
                "a universe view requires at least one source",
            ));
        }
        Ok(Self {
            clock,
            sources,
            composition,
        })
    }

    /// The current engine clock head.
    pub fn head(&self) -> DateTime {
        self.clock.now()
    }

    /// The composition rule.
    pub fn composition(&self) -> UniverseComposition {
        self.composition
    }

    /// The selectable members as of `as_of`, clipped to the head.
    pub fn resolve(&self, as_of: Option<DateTime>) -> Result<UniverseSnapshot, DataError> {
        let head = self.clock.now().to_nanos();
        let requested = match as_of {
            None => head,
            Some(value) => value.to_nanos(),
        };
        if requested > head {
            return Err(DataError::Lookahead(format!(
                "as_of {requested} is newer than the engine clock head {head}"
            )));
        }
        let engine_date = DateTime::from_nanos(requested).date();
        let per_source: Vec<Vec<UniverseMember>> = self
            .sources
            .iter()
            .map(|source| source.members(engine_date))
            .collect::<Result<_, _>>()?;
        Ok(UniverseSnapshot::new(
            engine_date,
            self.composition,
            self.compose(&per_source),
        ))
    }

    /// Whether `symbol` is selectable on `as_of`.
    pub fn membership(&self, symbol: &str, as_of: Option<DateTime>) -> Result<bool, DataError> {
        Ok(self.resolve(as_of)?.membership(symbol))
    }

    /// The member symbols on `as_of` in deterministic order.
    pub fn symbols(&self, as_of: Option<DateTime>) -> Result<Vec<String>, DataError> {
        Ok(self.resolve(as_of)?.symbols())
    }

    fn compose(&self, per_source: &[Vec<UniverseMember>]) -> Vec<UniverseMember> {
        match self.composition {
            UniverseComposition::Union => per_source.iter().flatten().cloned().collect(),
            UniverseComposition::Intersection => {
                let mut shared: HashSet<String> = per_source[0]
                    .iter()
                    .map(|member| member.symbol().to_string())
                    .collect();
                for members in &per_source[1..] {
                    let symbols: HashSet<String> = members
                        .iter()
                        .map(|member| member.symbol().to_string())
                        .collect();
                    shared = shared.intersection(&symbols).cloned().collect();
                }
                if shared.is_empty() {
                    return Vec::new();
                }
                let mut chosen: std::collections::HashSet<String> = HashSet::new();
                let mut result = Vec::new();
                for members in per_source {
                    for member in members {
                        if shared.contains(member.symbol())
                            && chosen.insert(member.symbol().to_string())
                        {
                            result.push(member.clone());
                        }
                    }
                }
                result
            }
        }
    }
}

#[cfg(test)]
#[path = "../tests/unit/universe_view.rs"]
mod tests;
