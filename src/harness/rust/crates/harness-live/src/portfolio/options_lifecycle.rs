//! Deterministic option expiry, exercise, and assignment settlement.
//!
//! Contract terms derive from canonical OSI symbols and all timing comes from
//! an injected `today` epoch-day value, so the lifecycle never consults a
//! wall clock. Physical exercise and assignment settle as exact underlying
//! fills at the strike.

use std::collections::{HashMap, HashSet};

use harness_num::Decimal18;

use crate::accounting::Fill;
use crate::error::{LiveError, LiveResult};
use crate::orders::Side;
use crate::portfolio::book::PositionSnapshot;
use crate::portfolio::hub::PortfolioHub;
use crate::symbol::{canonical_symbol, OsiSymbol, Symbol};

const ZERO: Decimal18 = Decimal18::ZERO;

/// How one contract position ended at expiration.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum SettlementKind {
    /// Long in-the-money exercise.
    Exercised,
    /// Short in-the-money assignment.
    Assigned,
    /// Out-of-the-money or pinned expiry.
    Worthless,
}

/// Portion of an expiring short contract position that is assigned.
pub trait AssignmentPolicy {
    /// Return the exact contract quantity assigned at expiration.
    fn assigned_quantity(
        &self,
        contract: &OsiSymbol,
        spot: Decimal18,
        short_quantity: Decimal18,
    ) -> Decimal18;
}

/// Assign one hundred percent of ITM short contracts and nothing else.
#[derive(Clone, Copy, Debug, Default)]
pub struct PessimisticAssignment;

impl AssignmentPolicy for PessimisticAssignment {
    fn assigned_quantity(
        &self,
        contract: &OsiSymbol,
        spot: Decimal18,
        short_quantity: Decimal18,
    ) -> Decimal18 {
        match intrinsic_value(contract, spot) {
            Ok(value) if value.is_positive() => short_quantity,
            _ => ZERO,
        }
    }
}

/// The exact per-share intrinsic value of `contract` at `spot`.
pub fn intrinsic_value(contract: &OsiSymbol, spot: Decimal18) -> LiveResult<Decimal18> {
    if !spot.is_positive() {
        return Err(LiveError::Portfolio("spot must be positive".into()));
    }
    let strike = contract.strike();
    if contract.option_type() == 'C' {
        let value = spot - strike;
        Ok(if value.is_positive() { value } else { ZERO })
    } else {
        let value = strike - spot;
        Ok(if value.is_positive() { value } else { ZERO })
    }
}

/// One deterministic days-to-expiry notification for an open contract.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DaysToExpiry {
    /// The expiring contract.
    pub symbol: OsiSymbol,
    /// Whole calendar days to expiry.
    pub days_to_expiry: i64,
    /// The threshold that triggered the event.
    pub threshold: i64,
    /// The observation day as epoch days.
    pub timestamp_days: i64,
}

/// Immutable result of settling one contract position in one book.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContractSettlement {
    /// The settled contract.
    pub symbol: OsiSymbol,
    /// The settled book identifier.
    pub book_id: String,
    /// How the position ended.
    pub kind: SettlementKind,
    /// Contracts moved through exercise or assignment.
    pub contracts: Decimal18,
    /// The underlying spot used.
    pub spot: Decimal18,
    /// Exact cash applied at option-ledger retirement.
    pub settlement_value: Decimal18,
    /// The generated underlying fill, when shares moved.
    pub underlying_fill: Option<Fill>,
    /// The retired option position snapshot.
    pub option_position: PositionSnapshot,
}

/// Settle option exercise, assignment, and worthless expiry exactly.
pub struct OptionLifecycle {
    hub: PortfolioHub,
    today_days: i64,
    multiplier: Decimal18,
    assignment_policy: Box<dyn AssignmentPolicy>,
    dte_thresholds: Vec<i64>,
    settlement_fee: Decimal18,
    retired: HashSet<Symbol>,
    retired_order: Vec<Symbol>,
    emitted: HashMap<Symbol, HashSet<i64>>,
    sequence: i64,
}

impl OptionLifecycle {
    /// Construct a lifecycle over an owned portfolio hub.
    pub fn new(
        hub: PortfolioHub,
        today_days: i64,
        multiplier: Decimal18,
        assignment_policy: Option<Box<dyn AssignmentPolicy>>,
        dte_thresholds: Vec<i64>,
        settlement_fee: Decimal18,
    ) -> LiveResult<Self> {
        if !multiplier.is_positive() {
            return Err(LiveError::Portfolio("multiplier must be positive".into()));
        }
        if settlement_fee.is_negative() {
            return Err(LiveError::Portfolio(
                "settlement_fee must not be negative".into(),
            ));
        }
        let mut thresholds = Vec::new();
        for threshold in dte_thresholds {
            if threshold < 0 {
                return Err(LiveError::Portfolio(
                    "dte thresholds must be non-negative integers".into(),
                ));
            }
            if !thresholds.contains(&threshold) {
                thresholds.push(threshold);
            }
        }
        thresholds.sort_unstable_by(|left, right| right.cmp(left));
        Ok(Self {
            hub,
            today_days,
            multiplier,
            assignment_policy: assignment_policy.unwrap_or_else(|| Box::new(PessimisticAssignment)),
            dte_thresholds: thresholds,
            settlement_fee,
            retired: HashSet::new(),
            retired_order: Vec::new(),
            emitted: HashMap::new(),
            sequence: 0,
        })
    }

    /// The portfolio hub that owns the settled books.
    pub fn hub(&self) -> &PortfolioHub {
        &self.hub
    }

    /// Mutable access to the portfolio hub.
    pub fn hub_mut(&mut self) -> &mut PortfolioHub {
        &mut self.hub
    }

    /// The contracts-to-shares multiplier used at settlement.
    pub fn multiplier(&self) -> Decimal18 {
        self.multiplier
    }

    /// Configured days-to-expiry thresholds in emission order.
    pub fn dte_thresholds(&self) -> &[i64] {
        &self.dte_thresholds
    }

    /// Retired contracts in retirement order.
    pub fn retired_symbols(&self) -> &[Symbol] {
        &self.retired_order
    }

    /// Open OSI contracts across books in canonical symbol order.
    pub fn open_contracts(&self) -> Vec<OsiSymbol> {
        let mut contracts: Vec<OsiSymbol> = self.open_interest().keys().cloned().collect();
        contracts.sort();
        contracts
    }

    /// Whether the contract has been settled and retired.
    pub fn is_retired(&self, symbol: &str) -> bool {
        canonical_symbol(symbol)
            .map(|canonical| self.retired.contains(&canonical))
            .unwrap_or(false)
    }

    /// Whole calendar days from the observation day to expiration.
    pub fn days_to_expiry(&self, symbol: &str) -> LiveResult<i64> {
        let contract = require_osi(symbol)?;
        Ok(expiration_days(&contract) - self.today_days)
    }

    /// Reject trading a retired contract or one already past expiration.
    pub fn ensure_tradable(&self, symbol: &str) -> LiveResult<()> {
        let canonical = canonical_symbol(symbol)?;
        if self.retired.contains(&canonical) {
            return Err(LiveError::Portfolio(format!(
                "contract {canonical:?} is retired and rejects new orders"
            )));
        }
        if let Ok(contract) = require_osi(symbol) {
            if expiration_days(&contract) < self.today_days {
                return Err(LiveError::Portfolio(format!(
                    "contract {:?} expired and rejects new orders",
                    contract.as_str()
                )));
            }
        }
        Ok(())
    }

    /// Route one fill through the hub, rejecting expired contracts first.
    pub fn apply_fill(&mut self, book_id: &str, fill: &Fill) -> LiveResult<PositionSnapshot> {
        self.ensure_tradable(fill.symbol.as_str())?;
        self.hub.apply_fill(book_id, fill)
    }

    /// Return and record new days-to-expiry events for open contracts.
    pub fn poll_dte_events(&mut self) -> Vec<DaysToExpiry> {
        let mut events = Vec::new();
        let mut contracts: Vec<OsiSymbol> = self.open_interest().keys().cloned().collect();
        contracts.sort();
        for contract in contracts {
            let days = expiration_days(&contract) - self.today_days;
            if days < 0 {
                continue;
            }
            let symbol = contract.to_symbol();
            for threshold in self.dte_thresholds.clone() {
                let emitted = self.emitted.entry(symbol.clone()).or_default();
                if days <= threshold && !emitted.contains(&threshold) {
                    emitted.insert(threshold);
                    events.push(DaysToExpiry {
                        symbol: contract.clone(),
                        days_to_expiry: days,
                        threshold,
                        timestamp_days: self.today_days,
                    });
                }
            }
        }
        events
    }

    /// Settle every at-or-past-expiration contract across all books.
    pub fn settle_expired(
        &mut self,
        spots: &HashMap<String, Decimal18>,
    ) -> LiveResult<Vec<ContractSettlement>> {
        let interest = self.open_interest();
        let mut expiring: Vec<(OsiSymbol, Vec<(String, Decimal18)>)> = interest
            .into_iter()
            .filter(|(contract, _)| expiration_days(contract) <= self.today_days)
            .collect();
        if expiring.is_empty() {
            return Ok(Vec::new());
        }
        expiring.sort_by(|left, right| left.0.cmp(&right.0));

        let mut spot_by_root: HashMap<String, Decimal18> = HashMap::new();
        for (root, spot) in spots {
            if !spot.is_positive() {
                return Err(LiveError::Portfolio("spot must be positive".into()));
            }
            spot_by_root.insert(root.clone(), *spot);
        }
        let mut missing: Vec<String> = expiring
            .iter()
            .filter(|(contract, _)| !spot_by_root.contains_key(contract.root()))
            .map(|(contract, _)| contract.root().to_string())
            .collect();
        missing.sort();
        missing.dedup();
        if !missing.is_empty() {
            return Err(LiveError::Portfolio(format!(
                "expiry settlement requires spots for underlying roots: {}",
                missing.join(", ")
            )));
        }

        let mut settlements = Vec::new();
        for (contract, holders) in expiring {
            let spot = spot_by_root[contract.root()];
            for (book_id, quantity) in holders {
                settlements.push(self.settle(&book_id, &contract, quantity, spot)?);
            }
            let symbol = contract.to_symbol();
            if self.retired.insert(symbol.clone()) {
                self.retired_order.push(symbol);
            }
        }
        Ok(settlements)
    }

    fn settle(
        &mut self,
        book_id: &str,
        contract: &OsiSymbol,
        quantity: Decimal18,
        spot: Decimal18,
    ) -> LiveResult<ContractSettlement> {
        let is_call = contract.option_type() == 'C';
        let intrinsic = intrinsic_value(contract, spot)?;
        let (kind, contracts, settlement_value, side) = if quantity.is_positive() {
            let exercised = if intrinsic.is_positive() {
                quantity
            } else {
                ZERO
            };
            let value = intrinsic * (quantity - exercised) * self.multiplier;
            (
                if exercised.is_positive() {
                    SettlementKind::Exercised
                } else {
                    SettlementKind::Worthless
                },
                exercised,
                value,
                if is_call { Side::Buy } else { Side::Sell },
            )
        } else {
            let short_quantity = -quantity;
            let assigned = self
                .assignment_policy
                .assigned_quantity(contract, spot, short_quantity);
            if assigned.is_negative() || assigned > short_quantity {
                return Err(LiveError::Portfolio(format!(
                    "assignment policy returned {assigned} contracts for {} short {short_quantity}",
                    contract.as_str()
                )));
            }
            let unassigned = short_quantity - assigned;
            let value = -(intrinsic * unassigned * self.multiplier);
            (
                if assigned.is_positive() {
                    SettlementKind::Assigned
                } else {
                    SettlementKind::Worthless
                },
                assigned,
                value,
                if is_call { Side::Sell } else { Side::Buy },
            )
        };

        let mut underlying_fill = None;
        if contracts.is_positive() {
            let fill = self.settlement_fill(book_id, contract, side, contracts)?;
            self.hub.apply_fill(book_id, &fill)?;
            underlying_fill = Some(fill);
        }
        let option_position =
            self.hub
                .retire_position(book_id, contract.as_str(), settlement_value)?;
        Ok(ContractSettlement {
            symbol: contract.clone(),
            book_id: book_id.to_string(),
            kind,
            contracts,
            spot,
            settlement_value,
            underlying_fill,
            option_position,
        })
    }

    fn settlement_fill(
        &mut self,
        book_id: &str,
        contract: &OsiSymbol,
        side: Side,
        contracts: Decimal18,
    ) -> LiveResult<Fill> {
        let fill_id = format!("settle:{}:{}", contract.as_str(), book_id);
        let order_id = format!("settle:{}", contract.as_str());
        let quantity = contracts * self.multiplier;
        let fill = Fill::new(
            &fill_id,
            &order_id,
            Symbol::new(contract.root())?,
            side,
            quantity,
            contract.strike(),
            self.settlement_fee,
            None,
        )?;
        self.sequence += 1;
        Ok(fill)
    }

    fn open_interest(&self) -> HashMap<OsiSymbol, Vec<(String, Decimal18)>> {
        let mut interest: HashMap<OsiSymbol, Vec<(String, Decimal18)>> = HashMap::new();
        for book in self.hub.books() {
            for position in book.positions() {
                if position.quantity == ZERO {
                    continue;
                }
                if let Ok(contract) = OsiSymbol::new(position.symbol.as_str()) {
                    interest
                        .entry(contract)
                        .or_default()
                        .push((book.book_id().to_string(), position.quantity));
                }
            }
        }
        interest
    }
}

fn require_osi(symbol: &str) -> LiveResult<OsiSymbol> {
    OsiSymbol::new(symbol)
        .map_err(|_| LiveError::Portfolio(format!("not an OSI option symbol: {symbol:?}")))
}

fn expiration_days(contract: &OsiSymbol) -> i64 {
    let raw = contract.expiration().as_bytes();
    let yy: i64 = format!("{}{}", raw[0] as char, raw[1] as char)
        .parse()
        .unwrap_or(0);
    let mm: i64 = format!("{}{}", raw[2] as char, raw[3] as char)
        .parse()
        .unwrap_or(1);
    let dd: i64 = format!("{}{}", raw[4] as char, raw[5] as char)
        .parse()
        .unwrap_or(1);
    let year = if yy <= 68 { 2000 + yy } else { 1900 + yy };
    days_from_civil(year, mm, dd)
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let adjusted_month = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * adjusted_month + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use harness_num::Decimal18;

    use super::{intrinsic_value, OptionLifecycle, SettlementKind};
    use crate::accounting::Fill;
    use crate::orders::Side;
    use crate::portfolio::hub::PortfolioHub;
    use crate::symbol::{OsiSymbol, Symbol};

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    // 2024-01-15 as epoch days.
    const TODAY: i64 = 19_737;

    fn lifecycle() -> OptionLifecycle {
        let mut hub = PortfolioHub::new("acct", vec![]).unwrap();
        hub.create_book("book", dec("100000")).unwrap();
        OptionLifecycle::new(hub, TODAY, dec("100"), None, vec![7, 1], dec("0")).unwrap()
    }

    fn option_fill(symbol: &str, side: Side, quantity: &str, price: &str) -> Fill {
        Fill::new(
            "f1",
            "o1",
            Symbol::new(symbol).unwrap(),
            side,
            dec(quantity),
            dec(price),
            Decimal18::ZERO,
            None,
        )
        .unwrap()
    }

    #[test]
    fn intrinsic_value_matches_call_and_put_payoffs() {
        let call = OsiSymbol::new("AAPL240119C00150000").unwrap();
        assert_eq!(intrinsic_value(&call, dec("155")).unwrap(), dec("5"));
        assert_eq!(intrinsic_value(&call, dec("145")).unwrap(), Decimal18::ZERO);
        let put = OsiSymbol::new("AAPL240119P00150000").unwrap();
        assert_eq!(intrinsic_value(&put, dec("145")).unwrap(), dec("5"));
        assert_eq!(intrinsic_value(&put, dec("155")).unwrap(), Decimal18::ZERO);
    }

    #[test]
    fn long_itm_call_exercises_into_underlying_fill() {
        let mut lifecycle = lifecycle();
        lifecycle
            .apply_fill(
                "book",
                &option_fill("AAPL240115C00150000", Side::Buy, "2", "600"),
            )
            .unwrap();
        let mut spots = HashMap::new();
        spots.insert("AAPL".to_string(), dec("160"));
        let settlements = lifecycle.settle_expired(&spots).unwrap();
        assert_eq!(settlements.len(), 1);
        let settlement = &settlements[0];
        assert_eq!(settlement.kind, SettlementKind::Exercised);
        assert_eq!(settlement.contracts, dec("2"));
        let fill = settlement.underlying_fill.as_ref().unwrap();
        assert_eq!(fill.side, Side::Buy);
        assert_eq!(fill.quantity, dec("200"));
        assert_eq!(fill.price, dec("150"));
        assert!(
            lifecycle
                .hub()
                .book("book")
                .unwrap()
                .position("AAPL")
                .unwrap()
                .quantity
                == dec("200")
        );
        assert!(lifecycle.is_retired("AAPL240115C00150000"));
    }

    #[test]
    fn short_itm_call_is_assigned_and_settles_cash() {
        let mut lifecycle = lifecycle();
        lifecycle
            .apply_fill(
                "book",
                &option_fill("AAPL240115C00150000", Side::Sell, "1", "600"),
            )
            .unwrap();
        let mut spots = HashMap::new();
        spots.insert("AAPL".to_string(), dec("160"));
        let settlements = lifecycle.settle_expired(&spots).unwrap();
        let settlement = &settlements[0];
        assert_eq!(settlement.kind, SettlementKind::Assigned);
        assert_eq!(settlement.contracts, dec("1"));
        assert_eq!(settlement.settlement_value, Decimal18::ZERO);
        assert_eq!(
            settlement.underlying_fill.as_ref().unwrap().side,
            Side::Sell
        );
    }

    #[test]
    fn dte_events_emit_once_per_threshold() {
        let mut lifecycle = lifecycle();
        // Contract expires 2024-01-19, three days after TODAY.
        lifecycle
            .apply_fill(
                "book",
                &option_fill("AAPL240119C00150000", Side::Buy, "1", "600"),
            )
            .unwrap();
        let first = lifecycle.poll_dte_events();
        assert_eq!(first.len(), 1);
        assert_eq!(first[0].threshold, 7);
        assert_eq!(first[0].days_to_expiry, 4);
        let second = lifecycle.poll_dte_events();
        assert!(second.is_empty());
    }
}
