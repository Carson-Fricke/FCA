//! Point-in-time security master over the market-lake listings table.
//!
//! Port of `harness.data.security_master`. The master answers one question per
//! engine date: what is known about a security as of that date. Membership is
//! loaded from the listings table without ever consulting rows dated after the
//! engine date, so future episodes stay invisible and future delistings keep a
//! listing active.

use std::rc::Rc;

use num::Decimal18;

use crate::catalog::{DelistingType, ListingRecord, ListingsCatalogPort};
use crate::clock::Clock;
use crate::error::DataError;
use crate::symbol::canonical_symbol;
use crate::time::{Date, DateTime};

fn canonical_entity(entity_id: &str) -> Result<String, DataError> {
    let canonical = entity_id.trim().to_uppercase();
    if canonical.is_empty() {
        return Err(DataError::value("entity_id must not be empty"));
    }
    Ok(canonical)
}

/// The security master facts proven for one entity on one engine date.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SecuritySnapshot {
    /// Canonical entity identity.
    pub entity_id: String,
    /// Engine date the snapshot was clipped to.
    pub as_of: Date,
    /// Symbol the snapshot was queried under.
    pub queried_symbol: String,
    /// Known episodes, oldest first.
    pub episodes: Vec<ListingRecord>,
}

impl SecuritySnapshot {
    /// Build a snapshot, clipping episodes listed after `as_of`.
    pub fn new(
        entity_id: &str,
        as_of: Date,
        queried_symbol: &str,
        episodes: Vec<ListingRecord>,
    ) -> Result<Self, DataError> {
        let mut known: Vec<ListingRecord> = episodes
            .into_iter()
            .filter(|episode| episode.listed_date <= as_of)
            .collect();
        if known.is_empty() {
            return Err(DataError::value(
                "a security snapshot requires at least one known episode",
            ));
        }
        known.sort_by(|a, b| (a.listed_date, &a.symbol).cmp(&(b.listed_date, &b.symbol)));
        Ok(Self {
            entity_id: canonical_entity(entity_id)?,
            as_of,
            queried_symbol: canonical_symbol(queried_symbol)?,
            episodes: known,
        })
    }

    fn active(&self) -> Option<&ListingRecord> {
        self.episodes
            .iter()
            .rev()
            .find(|episode| episode.active_on(self.as_of, self.as_of))
    }

    /// The symbol under which the entity trades on `as_of`.
    pub fn symbol(&self) -> String {
        match self.active() {
            Some(active) => active.symbol.clone(),
            None => self.episodes[self.episodes.len() - 1].symbol.clone(),
        }
    }

    /// Whether the entity had an active listing on `as_of`.
    pub fn is_listed(&self) -> bool {
        self.active().is_some()
    }

    /// The earliest listing date known on `as_of`.
    pub fn listed_date(&self) -> Date {
        self.episodes[0].listed_date
    }

    /// The delisting date proven on `as_of`, if any.
    pub fn delisted_date(&self) -> Option<Date> {
        if self.is_listed() {
            return None;
        }
        let latest = self.episodes.last().expect("non-empty snapshot");
        match latest.delisted_date {
            Some(date) if date <= self.as_of => Some(date),
            _ => None,
        }
    }

    /// The delisting type once the delisting is proven.
    pub fn delisting_type(&self) -> Option<DelistingType> {
        self.delisted_date()?;
        self.episodes
            .last()
            .expect("non-empty snapshot")
            .delisting_type
    }

    /// The final price once the delisting is proven.
    pub fn final_price(&self) -> Option<Decimal18> {
        self.delisted_date()?;
        self.episodes
            .last()
            .expect("non-empty snapshot")
            .final_price
    }

    /// The rename chain known on `as_of`, oldest first.
    pub fn symbols(&self) -> Vec<String> {
        self.episodes
            .iter()
            .map(|episode| episode.symbol.clone())
            .collect()
    }

    /// The episode covering `day`, which must not exceed `as_of`.
    pub fn episode_on(&self, day: Date) -> Result<Option<&ListingRecord>, DataError> {
        if day > self.as_of {
            return Err(DataError::value(
                "day must be on or before the snapshot as_of date",
            ));
        }
        Ok(self
            .episodes
            .iter()
            .rev()
            .find(|episode| episode.active_on(day, self.as_of)))
    }
}

/// Point-in-time security master clipped to the engine clock.
pub struct SecurityMasterView {
    clock: Box<dyn Clock>,
    listings: Rc<dyn ListingsCatalogPort>,
}

impl SecurityMasterView {
    /// Build a view on `clock` reading only `listings`.
    pub fn new(clock: Box<dyn Clock>, listings: Rc<dyn ListingsCatalogPort>) -> Self {
        Self { clock, listings }
    }

    /// The current engine clock head.
    pub fn head(&self) -> DateTime {
        self.clock.now()
    }

    /// The security known under `symbol` as of `as_of`.
    pub fn resolve(
        &self,
        symbol: &str,
        as_of: Option<DateTime>,
    ) -> Result<Option<SecuritySnapshot>, DataError> {
        let queried = canonical_symbol(symbol)?;
        let engine_date = self.engine_date(as_of)?;
        let episodes = self.visible(Some(&queried), None, engine_date)?;
        let Some(chosen) = episodes
            .iter()
            .max_by(|a, b| (a.listed_date, &a.entity_id).cmp(&(b.listed_date, &b.entity_id)))
        else {
            return Ok(None);
        };
        let entity = chosen.entity_id.clone();
        let chain = self.visible(None, Some(&entity), engine_date)?;
        Ok(Some(SecuritySnapshot::new(
            &entity,
            engine_date,
            &queried,
            chain,
        )?))
    }

    /// The entity identity behind `symbol` as of `as_of`.
    pub fn entity_id(
        &self,
        symbol: &str,
        as_of: Option<DateTime>,
    ) -> Result<Option<String>, DataError> {
        Ok(self
            .resolve(symbol, as_of)?
            .map(|snapshot| snapshot.entity_id))
    }

    /// Whether `symbol` proved listed on `as_of`.
    pub fn is_listed(&self, symbol: &str, as_of: Option<DateTime>) -> Result<bool, DataError> {
        Ok(self
            .resolve(symbol, as_of)?
            .is_some_and(|snapshot| snapshot.is_listed()))
    }

    /// The symbol the entity traded under on `as_of`, if any.
    pub fn active_symbol(
        &self,
        entity_id: &str,
        as_of: Option<DateTime>,
    ) -> Result<Option<String>, DataError> {
        let engine_date = self.engine_date(as_of)?;
        let episodes = self.visible(None, Some(&canonical_entity(entity_id)?), engine_date)?;
        Ok(episodes
            .iter()
            .rev()
            .find(|episode| episode.active_on(engine_date, engine_date))
            .map(|episode| episode.symbol.clone()))
    }

    /// The tradable symbols on `as_of`, one per listed entity.
    pub fn listed_symbols(&self, as_of: Option<DateTime>) -> Result<Vec<String>, DataError> {
        let engine_date = self.engine_date(as_of)?;
        let mut active: std::collections::HashMap<String, ListingRecord> =
            std::collections::HashMap::new();
        for episode in self.visible(None, None, engine_date)? {
            if episode.active_on(engine_date, engine_date) {
                active.insert(episode.entity_id.clone(), episode);
            }
        }
        let mut symbols: Vec<String> = active.into_values().map(|episode| episode.symbol).collect();
        symbols.sort();
        symbols.dedup();
        Ok(symbols)
    }

    /// The entity's rename chain known on `as_of`.
    pub fn history(
        &self,
        entity_id: &str,
        as_of: Option<DateTime>,
    ) -> Result<Vec<ListingRecord>, DataError> {
        let engine_date = self.engine_date(as_of)?;
        self.visible(None, Some(&canonical_entity(entity_id)?), engine_date)
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

    fn visible(
        &self,
        symbol: Option<&str>,
        entity_id: Option<&str>,
        engine_date: Date,
    ) -> Result<Vec<ListingRecord>, DataError> {
        let mut episodes = self
            .listings
            .list(entity_id, symbol, None)?
            .into_iter()
            .filter(|episode| episode.listed_date <= engine_date)
            .collect::<Vec<_>>();
        episodes.sort_by(|a, b| (a.listed_date, &a.symbol).cmp(&(b.listed_date, &b.symbol)));
        Ok(episodes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::catalog::{ListingType, MemoryListingsCatalog};
    use crate::clock::ManualClock;
    use std::str::FromStr;

    fn date(year: i32, month: u32, day: u32) -> Date {
        Date::from_ymd(year, month, day).unwrap()
    }

    fn instant(year: i32, month: u32, day: u32) -> DateTime {
        DateTime::from_ymd_hms(year, month, day, 12, 0, 0, 0).unwrap()
    }

    fn listing(entity: &str, symbol: &str, listed: Date) -> ListingRecord {
        ListingRecord::new(
            entity,
            symbol,
            listed,
            None,
            ListingType::Unknown,
            None,
            None,
        )
        .unwrap()
    }

    #[test]
    fn resolve_hides_future_and_heals_renames() {
        let catalog = MemoryListingsCatalog {
            episodes: vec![
                listing("AA", "AAA", date(2020, 1, 1)),
                ListingRecord::new(
                    "AA",
                    "AAB",
                    date(2021, 1, 1),
                    None,
                    ListingType::Rename,
                    None,
                    None,
                )
                .unwrap(),
            ],
        };
        let view = SecurityMasterView::new(
            Box::new(ManualClock::new(instant(2022, 6, 1))),
            Rc::new(catalog),
        );
        let snapshot = view.resolve("AAB", None).unwrap().unwrap();
        assert_eq!(snapshot.entity_id, "AA");
        assert_eq!(snapshot.symbols(), vec!["AAA", "AAB"]);
        assert_eq!(snapshot.symbol(), "AAB");
        assert!(view.is_listed("AAA", None).unwrap());
        // Before the successor episode lists, the symbol is unknown entirely.
        assert!(view
            .resolve("AAB", Some(instant(2020, 6, 1)))
            .unwrap()
            .is_none());
    }

    #[test]
    fn delisting_final_price_requires_known_date() {
        let record = ListingRecord::new(
            "BB",
            "BBB",
            date(2020, 1, 1),
            Some(date(2022, 1, 1)),
            ListingType::Ipo,
            Some(DelistingType::Merger),
            Some(Decimal18::from_str("12.5").unwrap()),
        )
        .unwrap();
        let catalog = MemoryListingsCatalog {
            episodes: vec![record],
        };
        let view = SecurityMasterView::new(
            Box::new(ManualClock::new(instant(2023, 1, 1))),
            Rc::new(catalog),
        );
        let snapshot = view.resolve("BBB", None).unwrap().unwrap();
        assert!(!snapshot.is_listed());
        assert_eq!(snapshot.delisted_date(), Some(date(2022, 1, 1)));
        assert_eq!(
            snapshot.final_price(),
            Some(Decimal18::from_str("12.5").unwrap())
        );
    }
}
