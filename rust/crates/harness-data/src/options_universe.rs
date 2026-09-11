//! Point-in-time option contract universe with hard look-ahead clipping.
//!
//! Port of `harness.data.options_universe`. Membership is read from the
//! contracts catalog without ever consulting rows dated after the engine date;
//! DTE, strike, and moneyness filters are evaluated against the engine date
//! only.

use std::collections::BTreeMap;
use std::rc::Rc;

use harness_num::Decimal18;

use crate::catalog::{ContractRecord, ContractsCatalogPort};
use crate::clock::Clock;
use crate::error::DataError;
use crate::symbol::{canonical_osi_symbol, canonical_symbol};
use crate::time::{Date, DateTime};

/// How daily membership is sourced from the contracts catalog.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SelectionMode {
    /// Read the persisted daily front set for the engine date.
    FrontSet,
    /// Read a fixed set of named OSI symbols.
    StaticOsi,
}

impl SelectionMode {
    /// The persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::FrontSet => "front_set",
            Self::StaticOsi => "static_osi",
        }
    }
}

fn contract_sort_key(record: &ContractRecord) -> (Date, Decimal18, &'static str, &str) {
    (
        record.expiration_date,
        record.strike_price,
        record.option_type.value(),
        &record.symbol,
    )
}

fn is_visible(record: &ContractRecord, underlying: &str, as_of: Date) -> bool {
    record.underlying == underlying
        && as_of <= record.expiration_date
        && record.listed_date.is_none_or(|listed| as_of >= listed)
        && record
            .delisted_date
            .is_none_or(|delisted| as_of <= delisted)
}

/// Inclusive calendar-DTE bounds evaluated on the engine date.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct DteFilter {
    /// Inclusive minimum days to expiry.
    pub min_days: Option<u32>,
    /// Inclusive maximum days to expiry.
    pub max_days: Option<u32>,
}

impl DteFilter {
    /// Build a validated filter.
    pub fn new(min_days: Option<u32>, max_days: Option<u32>) -> Result<Self, DataError> {
        if let (Some(min), Some(max)) = (min_days, max_days) {
            if min > max {
                return Err(DataError::value("min_days must not exceed max_days"));
            }
        }
        Ok(Self { min_days, max_days })
    }

    /// Whether `dte` lies inside the inclusive bounds.
    pub fn matches(&self, dte: i64) -> bool {
        self.min_days.is_none_or(|low| dte >= low as i64)
            && self.max_days.is_none_or(|high| dte <= high as i64)
    }
}

/// Inclusive absolute strike bounds.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct StrikeFilter {
    /// Inclusive minimum strike.
    pub min_strike: Option<Decimal18>,
    /// Inclusive maximum strike.
    pub max_strike: Option<Decimal18>,
}

impl StrikeFilter {
    /// Build a validated filter.
    pub fn new(
        min_strike: Option<Decimal18>,
        max_strike: Option<Decimal18>,
    ) -> Result<Self, DataError> {
        if let (Some(low), Some(high)) = (min_strike, max_strike) {
            if low > high {
                return Err(DataError::value("min_strike must not exceed max_strike"));
            }
        }
        Ok(Self {
            min_strike,
            max_strike,
        })
    }

    /// Whether `strike` lies inside the inclusive bounds.
    pub fn matches(&self, strike: Decimal18) -> bool {
        self.min_strike.is_none_or(|low| strike >= low)
            && self.max_strike.is_none_or(|high| strike <= high)
    }
}

/// Inclusive strike-to-reference-price ratio bounds.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct MoneynessFilter {
    /// Inclusive minimum moneyness.
    pub min_moneyness: Option<Decimal18>,
    /// Inclusive maximum moneyness.
    pub max_moneyness: Option<Decimal18>,
}

impl MoneynessFilter {
    /// Build a validated filter.
    pub fn new(
        min_moneyness: Option<Decimal18>,
        max_moneyness: Option<Decimal18>,
    ) -> Result<Self, DataError> {
        if let (Some(low), Some(high)) = (min_moneyness, max_moneyness) {
            if low > high {
                return Err(DataError::value(
                    "min_moneyness must not exceed max_moneyness",
                ));
            }
        }
        Ok(Self {
            min_moneyness,
            max_moneyness,
        })
    }

    /// Whether `strike / reference_price` is inside the bounds.
    pub fn matches(
        &self,
        strike: Decimal18,
        reference_price: Decimal18,
    ) -> Result<bool, DataError> {
        let low_ok = match self.min_moneyness {
            None => true,
            Some(low) => strike >= low.checked_mul(reference_price)?,
        };
        let high_ok = match self.max_moneyness {
            None => true,
            Some(high) => strike <= high.checked_mul(reference_price)?,
        };
        Ok(low_ok && high_ok)
    }
}

/// Point-in-time reference data for an option's underlying instrument.
pub trait UnderlyingView {
    /// Whether the underlying had an active listing on `as_of`.
    fn is_listed(&self, underlying: &str, as_of: Date) -> bool;
    /// The underlying reference price known on `as_of`, if any.
    fn reference_price(&self, underlying: &str, as_of: Date) -> Option<Decimal18>;
}

/// Validated selection configuration for one option universe.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OptionUniverseSpec {
    /// Canonical underlying root.
    pub underlying: String,
    /// Membership source.
    pub mode: SelectionMode,
    /// Named OSI symbols for static mode.
    pub static_symbols: Vec<String>,
    /// Optional DTE filter.
    pub dte: Option<DteFilter>,
    /// Optional strike filter.
    pub strike: Option<StrikeFilter>,
    /// Optional moneyness filter.
    pub moneyness: Option<MoneynessFilter>,
}

impl OptionUniverseSpec {
    /// Build and validate a spec.
    pub fn new(
        underlying: &str,
        mode: SelectionMode,
        static_symbols: Vec<String>,
        dte: Option<DteFilter>,
        strike: Option<StrikeFilter>,
        moneyness: Option<MoneynessFilter>,
    ) -> Result<Self, DataError> {
        let mut symbols = Vec::with_capacity(static_symbols.len());
        for symbol in static_symbols {
            symbols.push(canonical_osi_symbol(&symbol)?);
        }
        symbols.sort();
        for window in symbols.windows(2) {
            if window[0] == window[1] {
                return Err(DataError::value(
                    "static_symbols must not contain duplicate OSI symbols",
                ));
            }
        }
        match mode {
            SelectionMode::StaticOsi if symbols.is_empty() => {
                return Err(DataError::value(
                    "static OSI mode requires at least one static symbol",
                ));
            }
            SelectionMode::FrontSet if !symbols.is_empty() => {
                return Err(DataError::value(
                    "front-set mode must not name static symbols",
                ));
            }
            _ => {}
        }
        Ok(Self {
            underlying: canonical_symbol(underlying)?,
            mode,
            static_symbols: symbols,
            dte,
            strike,
            moneyness,
        })
    }
}

/// The option contracts selectable for one underlying on one engine date.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OptionUniverseSnapshot {
    /// Canonical underlying root.
    pub underlying: String,
    /// Engine date.
    pub as_of: Date,
    /// Membership source.
    pub mode: SelectionMode,
    /// Visible contracts in deterministic order.
    pub contracts: Vec<ContractRecord>,
}

impl OptionUniverseSnapshot {
    /// Build a snapshot, clipping contracts not visible on `as_of`.
    pub fn new(
        underlying: &str,
        as_of: Date,
        mode: SelectionMode,
        contracts: Vec<ContractRecord>,
    ) -> Result<Self, DataError> {
        let underlying = canonical_symbol(underlying)?;
        let mut visible: Vec<ContractRecord> = contracts
            .into_iter()
            .filter(|record| is_visible(record, &underlying, as_of))
            .collect();
        visible.sort_by(|a, b| contract_sort_key(a).cmp(&contract_sort_key(b)));
        Ok(Self {
            underlying,
            as_of,
            mode,
            contracts: visible,
        })
    }

    /// Selectable expiration dates in ascending order.
    pub fn expiries(&self) -> Vec<Date> {
        let mut expiries: Vec<Date> = self
            .contracts
            .iter()
            .map(|record| record.expiration_date)
            .collect();
        expiries.sort();
        expiries.dedup();
        expiries
    }

    /// The earliest selectable expiration date, if any.
    pub fn front_expiry(&self) -> Option<Date> {
        self.expiries().into_iter().next()
    }

    /// The selectable contracts at the front expiration.
    pub fn front(&self) -> Vec<&ContractRecord> {
        match self.front_expiry() {
            None => Vec::new(),
            Some(expiry) => self.at_expiry(expiry),
        }
    }

    /// Selected OSI symbols in deterministic order.
    pub fn symbols(&self) -> Vec<String> {
        self.contracts
            .iter()
            .map(|record| record.symbol.clone())
            .collect()
    }

    /// The expiry-aware index used for resolution and retirement.
    pub fn by_expiry(&self) -> BTreeMap<Date, Vec<&ContractRecord>> {
        let mut grouped: BTreeMap<Date, Vec<&ContractRecord>> = BTreeMap::new();
        for record in &self.contracts {
            grouped
                .entry(record.expiration_date)
                .or_default()
                .push(record);
        }
        grouped
    }

    /// The contracts expiring on `expiry`, in sorted order.
    pub fn at_expiry(&self, expiry: Date) -> Vec<&ContractRecord> {
        self.contracts
            .iter()
            .filter(|record| record.expiration_date == expiry)
            .collect()
    }

    /// Calendar days from `as_of` to `expiry`.
    pub fn dte(&self, expiry: Date) -> i64 {
        i64::from(self.as_of.days_until(expiry))
    }

    /// One selected contract by OSI symbol, if selected.
    pub fn contract(&self, symbol: &str) -> Result<Option<&ContractRecord>, DataError> {
        let target = canonical_osi_symbol(symbol)?;
        Ok(self.contracts.iter().find(|record| record.symbol == target))
    }
}

/// Point-in-time option contract universe clipped to the engine clock.
pub struct OptionUniverseView {
    clock: Box<dyn Clock>,
    catalog: Rc<dyn ContractsCatalogPort>,
    spec: OptionUniverseSpec,
    underlyings: Option<Rc<dyn UnderlyingView>>,
}

impl OptionUniverseView {
    /// Build a view, requiring an underlying view when moneyness is filtered.
    pub fn new(
        clock: Box<dyn Clock>,
        catalog: Rc<dyn ContractsCatalogPort>,
        spec: OptionUniverseSpec,
        underlyings: Option<Rc<dyn UnderlyingView>>,
    ) -> Result<Self, DataError> {
        if spec.moneyness.is_some() && underlyings.is_none() {
            return Err(DataError::value(
                "moneyness filters require an underlying view",
            ));
        }
        Ok(Self {
            clock,
            catalog,
            spec,
            underlyings,
        })
    }

    /// The validated selection configuration.
    pub fn spec(&self) -> &OptionUniverseSpec {
        &self.spec
    }

    /// The current engine clock head.
    pub fn head(&self) -> DateTime {
        self.clock.now()
    }

    /// The selectable contracts as of `as_of`, clipped to the head.
    pub fn resolve(&self, as_of: Option<DateTime>) -> Result<OptionUniverseSnapshot, DataError> {
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
        let underlying = &self.spec.underlying;
        let listed = self
            .underlyings
            .as_ref()
            .is_none_or(|view| view.is_listed(underlying, engine_date));
        let contracts = if listed {
            let members = self.members(engine_date)?;
            self.filtered(&members, engine_date)?
        } else {
            Vec::new()
        };
        OptionUniverseSnapshot::new(underlying, engine_date, self.spec.mode, contracts)
    }

    fn members(&self, engine_date: Date) -> Result<Vec<ContractRecord>, DataError> {
        match self.spec.mode {
            SelectionMode::FrontSet => Ok(self
                .catalog
                .get_front_set(&self.spec.underlying, engine_date)?
                .map(|set| set.contracts)
                .unwrap_or_default()),
            SelectionMode::StaticOsi => {
                let mut members = Vec::new();
                for symbol in &self.spec.static_symbols {
                    if let Some(record) = self.catalog.get(symbol)? {
                        if record.underlying == self.spec.underlying {
                            members.push(record);
                        }
                    }
                }
                Ok(members)
            }
        }
    }

    fn filtered(
        &self,
        members: &[ContractRecord],
        engine_date: Date,
    ) -> Result<Vec<ContractRecord>, DataError> {
        let reference = if self.spec.moneyness.is_some() {
            let view = self
                .underlyings
                .as_ref()
                .expect("validated at construction");
            match view.reference_price(&self.spec.underlying, engine_date) {
                Some(price) if price.is_positive() => Some(price),
                _ => return Ok(Vec::new()),
            }
        } else {
            None
        };
        let mut selected = Vec::new();
        for record in members {
            if let Some(dte) = &self.spec.dte {
                let dte_days = i64::from(engine_date.days_until(record.expiration_date));
                if !dte.matches(dte_days) {
                    continue;
                }
            }
            if let Some(strike) = &self.spec.strike {
                if !strike.matches(record.strike_price) {
                    continue;
                }
            }
            if let (Some(moneyness), Some(reference)) = (&self.spec.moneyness, reference) {
                if !moneyness.matches(record.strike_price, reference)? {
                    continue;
                }
            }
            selected.push(record.clone());
        }
        Ok(selected)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::catalog::{MemoryContractsCatalog, OptionType};
    use crate::clock::ManualClock;
    use std::str::FromStr;

    fn date(year: i32, month: u32, day: u32) -> Date {
        Date::from_ymd(year, month, day).unwrap()
    }

    fn instant(year: i32, month: u32, day: u32) -> DateTime {
        DateTime::from_ymd_hms(year, month, day, 12, 0, 0, 0).unwrap()
    }

    fn contract(
        symbol: &str,
        expiry: Date,
        strike: &str,
        option_type: OptionType,
    ) -> ContractRecord {
        ContractRecord::new(
            symbol,
            "AAPL",
            expiry,
            Decimal18::from_str(strike).unwrap(),
            option_type,
            100,
            None,
            None,
        )
        .unwrap()
    }

    #[allow(clippy::too_many_arguments)]
    fn contract_full(
        symbol: &str,
        expiry: Date,
        strike: &str,
        option_type: OptionType,
        listed: Option<Date>,
        delisted: Option<Date>,
    ) -> ContractRecord {
        ContractRecord::new(
            symbol,
            "AAPL",
            expiry,
            Decimal18::from_str(strike).unwrap(),
            option_type,
            100,
            listed,
            delisted,
        )
        .unwrap()
    }

    /// Golden ordering and visibility from `options_universe._contract_sort_key`
    /// and `_is_visible` evaluated on the same catalog rows.
    #[test]
    fn snapshot_order_matches_python_sort_key() {
        let records = vec![
            contract(
                "AAPL240119C00150000",
                date(2024, 1, 19),
                "150",
                OptionType::Call,
            ),
            contract_full(
                "AAPL250119C00150000",
                date(2025, 1, 19),
                "150",
                OptionType::Call,
                Some(date(2025, 1, 1)),
                None,
            ),
            contract_full(
                "AAPL250119P00140000",
                date(2025, 1, 19),
                "140",
                OptionType::Put,
                None,
                Some(date(2024, 5, 1)),
            ),
            contract(
                "AAPL250119C00160000",
                date(2025, 1, 19),
                "160",
                OptionType::Call,
            ),
            contract(
                "AAPL250119C00140000",
                date(2025, 1, 19),
                "140",
                OptionType::Call,
            ),
            contract(
                "AAPL241220C00150000",
                date(2024, 12, 20),
                "150",
                OptionType::Call,
            ),
            contract(
                "AAPL250119P00140000",
                date(2025, 1, 19),
                "140",
                OptionType::Put,
            ),
        ];
        let snapshot = OptionUniverseSnapshot::new(
            "AAPL",
            date(2024, 6, 1),
            SelectionMode::StaticOsi,
            records,
        )
        .unwrap();
        assert_eq!(
            snapshot.symbols(),
            vec![
                "AAPL241220C00150000",
                "AAPL250119C00140000",
                "AAPL250119P00140000",
                "AAPL250119C00160000",
            ]
        );
        assert_eq!(snapshot.front_expiry(), Some(date(2024, 12, 20)));
    }

    #[test]
    fn static_universe_clips_to_engine_date() {
        let catalog = MemoryContractsCatalog {
            contracts: vec![
                contract(
                    "AAPL240119C00150000",
                    date(2024, 1, 19),
                    "150",
                    OptionType::Call,
                ),
                contract(
                    "AAPL250119C00150000",
                    date(2025, 1, 19),
                    "150",
                    OptionType::Call,
                ),
            ],
            front_sets: Vec::new(),
        };
        let spec = OptionUniverseSpec::new(
            "AAPL",
            SelectionMode::StaticOsi,
            vec![
                "AAPL240119C00150000".to_string(),
                "AAPL250119C00150000".to_string(),
            ],
            None,
            None,
            None,
        )
        .unwrap();
        let view = OptionUniverseView::new(
            Box::new(ManualClock::new(instant(2024, 6, 1))),
            Rc::new(catalog),
            spec,
            None,
        )
        .unwrap();
        let snapshot = view.resolve(None).unwrap();
        assert_eq!(snapshot.symbols(), vec!["AAPL250119C00150000"]);
        assert_eq!(snapshot.front_expiry(), Some(date(2025, 1, 19)));
    }
}
