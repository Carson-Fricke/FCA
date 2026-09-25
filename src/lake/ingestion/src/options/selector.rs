//! Deterministic daily option front-set selection.
//!
//! Ports `lake.ingestion.options.selector`. The selector is pure apart from an
//! explicit repository call: it accepts already normalized T024 contract
//! records and never constructs provider or storage clients, which keeps daily
//! membership reproducible in tests and workers.
//!
//! Semantics preserved exactly: expiration and strike boundaries are
//! inclusive, contracts outside their listed/delisted lifecycle or already
//! expired are excluded before the nearest-expiration and strike filters, and
//! the returned contracts are ordered by
//! `(expiration_date, strike_price, option_type, symbol)`.

use std::cmp::Ordering;
use std::collections::BTreeSet;

use lake_catalog::{ContractRecord, ContractRepository, ContractsCatalog, DailyFrontSet};
use lake_configuration::{OptionSelection, UniverseSettings};
use lake_contracts::{DataError, Date, Decimal18};
const ONE_UNSCALED: i128 = lake_contracts::decimal::ONE_UNSCALED;

/// Select the configured nearest expirations and spot-relative strikes.
#[derive(Clone, Debug)]
pub struct OptionChainSelector {
    selection: OptionSelection,
}

impl OptionChainSelector {
    /// Build a selector, rejecting any policy but daily nearest-by-spot.
    pub fn new(selection: OptionSelection) -> Result<Self, DataError> {
        if selection.frequency != "daily" {
            return Err(DataError::value("option selection frequency must be daily"));
        }
        if selection.expiration_order != "nearest" {
            return Err(DataError::value("option selection order must be nearest"));
        }
        if selection.strike_window.basis != "underlying_spot" {
            return Err(DataError::value(
                "strike window basis must be underlying_spot",
            ));
        }
        Ok(Self { selection })
    }

    /// Construct a selector from T026 typed universe settings.
    pub fn from_universes(settings: &UniverseSettings) -> Result<Self, DataError> {
        Self::new(settings.options.selection.clone())
    }

    /// The validated selection policy.
    pub fn selection(&self) -> &OptionSelection {
        &self.selection
    }

    /// Return the deterministic daily front set from candidate contracts.
    ///
    /// Expiration and strike boundaries are inclusive. Contracts that are
    /// expired or outside their listed/delisted lifecycle on `selection_date`
    /// are excluded before the nearest expiration and strike filters apply.
    pub fn select(
        &self,
        underlying: &str,
        selection_date: Date,
        spot: Decimal18,
        contracts: &[ContractRecord],
    ) -> Result<Vec<ContractRecord>, DataError> {
        let canonical = canonical_underlying(underlying)?;
        if !spot.is_positive() {
            return Err(DataError::value("spot must be a finite positive number"));
        }
        let (lower, upper) = self.strike_bounds(spot)?;

        let eligible: Vec<&ContractRecord> = contracts
            .iter()
            .filter(|contract| {
                contract.underlying == canonical
                    && contract.expiration_date >= selection_date
                    && active_on(contract, selection_date)
            })
            .collect();
        let expirations: BTreeSet<Date> = eligible
            .iter()
            .map(|contract| contract.expiration_date)
            .collect();
        let selected_expirations: BTreeSet<Date> = expirations
            .into_iter()
            .take(self.selection.expiration_count as usize)
            .collect();

        let mut selected: Vec<ContractRecord> = eligible
            .into_iter()
            .filter(|contract| {
                selected_expirations.contains(&contract.expiration_date)
                    && lower <= contract.strike_price
                    && contract.strike_price <= upper
            })
            .cloned()
            .collect();
        selected.sort_by(compare_contracts);
        Ok(selected)
    }

    /// Select from active T024 contract metadata for one date.
    pub fn select_from_repository(
        &self,
        repository: &dyn ContractRepository,
        underlying: &str,
        selection_date: Date,
        spot: Decimal18,
    ) -> Result<Vec<ContractRecord>, DataError> {
        let contracts = repository.list(Some(underlying), Some(selection_date))?;
        self.select(underlying, selection_date, spot, &contracts)
    }

    /// Select active metadata and replace that day's persisted front set.
    pub fn select_and_persist(
        &self,
        catalog: &ContractsCatalog,
        underlying: &str,
        selection_date: Date,
        spot: Decimal18,
    ) -> Result<DailyFrontSet, DataError> {
        let contracts = self.select_from_repository(
            catalog.contracts.as_ref(),
            underlying,
            selection_date,
            spot,
        )?;
        let front_set = DailyFrontSet::new(underlying, selection_date, contracts)?;
        catalog.upsert_front_set(&front_set)?;
        Ok(front_set)
    }

    /// The inclusive `[lower, upper]` strike window around `spot`.
    ///
    /// Mirrors Python's exact decimal arithmetic: each percent is divided by
    /// 100 as an exact scale-18 decimal, so `500 - 10%` is exactly `450` and a
    /// contract struck at the boundary is always selected.
    fn strike_bounds(&self, spot: Decimal18) -> Result<(Decimal18, Decimal18), DataError> {
        let below = percent_fraction(self.selection.strike_window.percent_below)?;
        let above = percent_fraction(self.selection.strike_window.percent_above)?;
        let lower = scale_amount(spot, ONE_UNSCALED - below)?;
        let upper = scale_amount(spot, ONE_UNSCALED + above)?;
        Ok((lower, upper))
    }
}

/// Compare two contracts on the pinned selection order.
pub(crate) fn compare_contracts(left: &ContractRecord, right: &ContractRecord) -> Ordering {
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

/// Whether the contract's listed/delisted lifecycle covers `selection_date`.
pub(crate) fn active_on(contract: &ContractRecord, selection_date: Date) -> bool {
    contract
        .listed_date
        .is_none_or(|listed| selection_date >= listed)
        && contract
            .delisted_date
            .is_none_or(|delisted| selection_date <= delisted)
}

/// Canonicalise a front-set underlying the way Python's selector does.
pub(crate) fn canonical_underlying(underlying: &str) -> Result<String, DataError> {
    let canonical = underlying.trim().to_uppercase();
    if canonical.is_empty() {
        return Err(DataError::value("underlying must not be empty"));
    }
    Ok(canonical)
}

/// Convert a configured percent into its scale-18 fraction of one.
///
/// Python computes `Decimal(str(percent)) / 100`; the shortest decimal
/// rendering of the float parses to the same value, and the division by 100
/// loses at most digits beyond scale 18.
fn percent_fraction(percent: f64) -> Result<i128, DataError> {
    if !percent.is_finite() {
        return Err(DataError::value("strike window percents must be finite"));
    }
    let parsed = Decimal18::parse(&format!("{percent}"))
        .map_err(|_| DataError::value("strike window percents must be finite"))?;
    Ok(parsed.unscaled() / 100)
}

/// Multiply `spot` by a scale-18 factor, dividing the product back to scale 18.
///
/// The operands are decomposed into mantissa and trailing-zero exponent first,
/// so `500 * 0.9` never forms the raw `4.5e38` product inside `i128`.
fn scale_amount(spot: Decimal18, factor: i128) -> Result<Decimal18, DataError> {
    let overflow =
        || DataError::Arithmetic("fixed-point value is outside decimal128(38, 18)".to_string());
    let (mantissa, exponent) = {
        let (m1, e1) = decompose(spot.unscaled());
        let (m2, e2) = decompose(factor);
        (
            m1.checked_mul(m2).ok_or_else(overflow)?,
            e1.saturating_add(e2),
        )
    };
    let unscaled = if exponent >= 18 {
        shift_mantissa(mantissa, exponent - 18).ok_or_else(overflow)?
    } else {
        shift_mantissa(mantissa, 18 - exponent).ok_or_else(overflow)?
    };
    Decimal18::from_unscaled(unscaled).map_err(|_| overflow())
}

/// Strip trailing zeros: the exact `(mantissa * 10^exponent)` decomposition.
fn decompose(mut value: i128) -> (i128, u32) {
    let mut exponent = 0u32;
    while value != 0 && value % 10 == 0 {
        value /= 10;
        exponent += 1;
    }
    (value, exponent)
}

/// Scale a mantissa by `10^exponent`, reporting overflow as `None`.
fn shift_mantissa(mut value: i128, exponent: u32) -> Option<i128> {
    for _ in 0..exponent {
        value = value.checked_mul(10)?;
    }
    Some(value)
}

#[cfg(test)]
#[path = "../../tests/unit/options_selector.rs"]
mod tests;
