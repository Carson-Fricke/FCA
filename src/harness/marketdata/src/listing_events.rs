//! Listing lifecycle events derived from the market-lake listings table.
//!
//! Port of `harness.data.listing_events`. The deriver turns listing episodes
//! into the two corporate-action facts a strategy must react to: terminal
//! delistings and symbol-preserving renames. Derivation is point in time and
//! mirrors the security master's no-look-ahead rule.

use std::collections::HashSet;

use num::Decimal18;

use crate::catalog::{DelistingType, ListingType, ListingsCatalogPort};
use crate::error::DataError;
use crate::symbol::canonical_symbol;
use crate::time::{Date, DateTime};

/// The listing lifecycle fact carried by one corporate-action event.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum CorporateActionKind {
    /// Terminal delisting.
    Delisting,
    /// Symbol-preserving rename.
    Rename,
}

impl CorporateActionKind {
    /// The persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Delisting => "delisting",
            Self::Rename => "rename",
        }
    }
}

fn canonical_entity(entity_id: &str) -> Result<String, DataError> {
    let canonical = entity_id.trim().to_uppercase();
    if canonical.is_empty() {
        return Err(DataError::value("entity_id must not be empty"));
    }
    Ok(canonical)
}

/// One listing lifecycle fact on one entity.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum CorporateActionEvent {
    /// One terminal delisting: the last session of one symbol episode.
    Delisting {
        /// Canonical entity identity.
        entity_id: String,
        /// Engine date on which the delisting became knowable.
        known_date: Date,
        /// Canonical symbol that delisted.
        symbol: String,
        /// Why the episode ended.
        delisting_type: DelistingType,
        /// Final print, if the market published one.
        final_price: Option<Decimal18>,
    },
    /// One symbol transition that preserves the entity identity.
    Rename {
        /// Canonical entity identity.
        entity_id: String,
        /// Successor's first session, when the rename became knowable.
        known_date: Date,
        /// Canonical predecessor symbol.
        old_symbol: String,
        /// Canonical successor symbol.
        new_symbol: String,
    },
}

impl CorporateActionEvent {
    /// The lifecycle fact this event carries.
    pub fn kind(&self) -> CorporateActionKind {
        match self {
            Self::Delisting { .. } => CorporateActionKind::Delisting,
            Self::Rename { .. } => CorporateActionKind::Rename,
        }
    }

    /// The symbol that orders this event within one entity.
    pub fn symbol_key(&self) -> &str {
        match self {
            Self::Delisting { symbol, .. } => symbol,
            Self::Rename { old_symbol, .. } => old_symbol,
        }
    }

    /// The canonical entity identity.
    pub fn entity_id(&self) -> &str {
        match self {
            Self::Delisting { entity_id, .. } | Self::Rename { entity_id, .. } => entity_id,
        }
    }

    /// The engine date on which the fact became knowable.
    pub fn known_date(&self) -> Date {
        match self {
            Self::Delisting { known_date, .. } | Self::Rename { known_date, .. } => *known_date,
        }
    }

    /// The deterministic delivery ordering key.
    pub fn sort_key(&self) -> (String, String, String, String) {
        (
            self.known_date().isoformat(),
            self.entity_id().to_string(),
            self.kind().value().to_string(),
            self.symbol_key().to_string(),
        )
    }

    /// Build a validated delisting event.
    pub fn delisting(
        entity_id: &str,
        known_date: Date,
        symbol: &str,
        delisting_type: DelistingType,
        final_price: Option<Decimal18>,
    ) -> Result<Self, DataError> {
        Ok(Self::Delisting {
            entity_id: canonical_entity(entity_id)?,
            known_date,
            symbol: canonical_symbol(symbol)?,
            delisting_type,
            final_price,
        })
    }

    /// Build a validated rename event.
    pub fn rename(
        entity_id: &str,
        known_date: Date,
        old_symbol: &str,
        new_symbol: &str,
    ) -> Result<Self, DataError> {
        let old = canonical_symbol(old_symbol)?;
        let new = canonical_symbol(new_symbol)?;
        if old == new {
            return Err(DataError::value("a rename must change the symbol"));
        }
        Ok(Self::Rename {
            entity_id: canonical_entity(entity_id)?,
            known_date,
            old_symbol: old,
            new_symbol: new,
        })
    }
}

/// Exact delisting settlement at the final price, pessimistic otherwise.
pub struct PessimisticDelistingSettlement;

impl PessimisticDelistingSettlement {
    /// Return the exact cash settled by closing `quantity` shares.
    pub fn settlement_value(
        quantity: Decimal18,
        final_price: Option<Decimal18>,
    ) -> Result<Decimal18, DataError> {
        if let Some(price) = final_price {
            if !price.is_positive() {
                return Err(DataError::value("final_price must be positive"));
            }
            return Ok(quantity.checked_mul(price)?);
        }
        if quantity.is_positive() {
            return Ok(Decimal18::ZERO);
        }
        if quantity.is_negative() {
            return Err(DataError::value(
                "pessimistic settlement of a short delisted position requires a known \
                 final_price",
            ));
        }
        Ok(Decimal18::ZERO)
    }
}

/// Derive due corporate-action events from the listings catalog.
pub struct ListingEventDeriver {
    clock: Box<dyn crate::clock::Clock>,
    listings: std::rc::Rc<dyn ListingsCatalogPort>,
    delivered: Vec<CorporateActionEvent>,
    emitted: HashSet<CorporateActionEvent>,
}

impl ListingEventDeriver {
    /// Build a deriver on `clock` reading only `listings`.
    pub fn new(
        clock: Box<dyn crate::clock::Clock>,
        listings: std::rc::Rc<dyn ListingsCatalogPort>,
    ) -> Self {
        Self {
            clock,
            listings,
            delivered: Vec::new(),
            emitted: HashSet::new(),
        }
    }

    /// Every event returned by [`Self::poll`], in delivery order.
    pub fn delivered(&self) -> &[CorporateActionEvent] {
        &self.delivered
    }

    /// Due events that have not been polled, without consuming.
    pub fn pending(&self, as_of: Option<DateTime>) -> Result<Vec<CorporateActionEvent>, DataError> {
        let engine_date = self.engine_date(as_of)?;
        let mut events: Vec<CorporateActionEvent> = self
            .derive(engine_date)?
            .into_iter()
            .filter(|event| !self.emitted.contains(event))
            .collect();
        events.sort_by_key(CorporateActionEvent::sort_key);
        Ok(events)
    }

    /// Due events in delivery order; records them as delivered.
    pub fn poll(
        &mut self,
        as_of: Option<DateTime>,
    ) -> Result<Vec<CorporateActionEvent>, DataError> {
        let events = self.pending(as_of)?;
        self.delivered.extend(events.iter().cloned());
        self.emitted.extend(events.iter().cloned());
        Ok(events)
    }

    fn engine_date(&self, as_of: Option<DateTime>) -> Result<Date, DataError> {
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
        Ok(DateTime::from_nanos(requested).date())
    }

    fn derive(&self, engine_date: Date) -> Result<Vec<CorporateActionEvent>, DataError> {
        let mut events = Vec::new();
        for chain in self.chains(engine_date)? {
            for (index, episode) in chain.iter().enumerate() {
                let predecessor = index.checked_sub(1).map(|previous| &chain[previous]);
                events.extend(self.episode_events(episode, predecessor, engine_date)?);
            }
        }
        Ok(events)
    }

    fn episode_events(
        &self,
        episode: &crate::catalog::ListingRecord,
        predecessor: Option<&crate::catalog::ListingRecord>,
        engine_date: Date,
    ) -> Result<Vec<CorporateActionEvent>, DataError> {
        let mut events = Vec::new();
        if episode.listing_type == ListingType::Rename {
            if let Some(predecessor) = predecessor {
                events.push(CorporateActionEvent::rename(
                    &episode.entity_id,
                    episode.listed_date,
                    &predecessor.symbol,
                    &episode.symbol,
                )?);
            }
        }
        if let Some(delisted) = episode.delisted_date {
            if delisted <= engine_date && episode.delisting_type != Some(DelistingType::Rename) {
                events.push(CorporateActionEvent::delisting(
                    &episode.entity_id,
                    delisted,
                    &episode.symbol,
                    episode.delisting_type.unwrap_or(DelistingType::Unknown),
                    episode.final_price,
                )?);
            }
        }
        Ok(events)
    }

    fn chains(
        &self,
        engine_date: Date,
    ) -> Result<Vec<Vec<crate::catalog::ListingRecord>>, DataError> {
        let mut chains: std::collections::HashMap<String, Vec<crate::catalog::ListingRecord>> =
            std::collections::HashMap::new();
        for episode in self.listings.list(None, None, None)? {
            if episode.listed_date > engine_date {
                continue;
            }
            chains
                .entry(episode.entity_id.clone())
                .or_default()
                .push(episode);
        }
        let mut result: Vec<Vec<crate::catalog::ListingRecord>> = chains
            .into_values()
            .map(|mut chain| {
                chain.sort_by(|a, b| (a.listed_date, &a.symbol).cmp(&(b.listed_date, &b.symbol)));
                chain
            })
            .collect();
        result.sort_by(|a, b| {
            let left = a
                .first()
                .map(|episode| (&episode.entity_id, episode.listed_date));
            let right = b
                .first()
                .map(|episode| (&episode.entity_id, episode.listed_date));
            left.cmp(&right)
        });
        Ok(result)
    }
}
