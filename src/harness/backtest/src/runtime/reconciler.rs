//! State reconstruction and break detection for paper/live parity.
//!
//! Deterministic core of `harness.runtime.reconciler`: tolerance-bounded
//! comparison of expected position and cash state against a broker snapshot,
//! plus the cadence at which reconciliation is due. Broker I/O stays behind
//! [`super::paper::PaperShell`] and the object-store ports.

use std::collections::BTreeMap;

use num::Decimal18;

/// What initiated one reconciliation pass.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReconcileTrigger {
    /// A periodic pass.
    Periodic,
    /// A session-end pass.
    SessionEnd,
    /// A stop pass.
    Stop,
}

impl ReconcileTrigger {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Periodic => "periodic",
            Self::SessionEnd => "session_end",
            Self::Stop => "stop",
        }
    }
}

/// The reconciler's verdict for one pass.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReconcileDecision {
    /// Expected and broker state agree within tolerance.
    Match,
    /// They diverge outside tolerance.
    Divergence,
}

/// Absolute tolerances applied to position and cash comparisons.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReconciliationTolerances {
    /// Maximum allowed absolute position difference.
    pub position_abs: Decimal18,
    /// Maximum allowed absolute cash difference.
    pub cash_abs: Decimal18,
}

impl Default for ReconciliationTolerances {
    fn default() -> Self {
        Self {
            position_abs: Decimal18::ZERO,
            cash_abs: Decimal18::ZERO,
        }
    }
}

/// One expected position.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExpectedPosition {
    /// Instrument symbol.
    pub symbol: String,
    /// String-exact signed quantity.
    pub quantity: String,
}

/// One broker-reported position.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BrokerPosition {
    /// Instrument symbol.
    pub symbol: String,
    /// String-exact signed quantity.
    pub quantity: String,
}

/// One position difference.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PositionDelta {
    /// Instrument symbol.
    pub symbol: String,
    /// String-exact expected quantity.
    pub expected: String,
    /// String-exact broker quantity.
    pub actual: String,
    /// String-exact difference.
    pub difference: String,
}

/// The full deterministic reconciliation difference.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReconciliationDiff {
    /// Position differences, sorted by symbol.
    pub positions: Vec<PositionDelta>,
    /// String-exact cash difference, if cash was supplied.
    pub cash: Option<String>,
}

impl ReconciliationDiff {
    /// Whether the diff is empty within tolerance.
    #[must_use]
    pub fn is_match(&self) -> bool {
        self.positions.is_empty() && self.cash.is_none()
    }
}

fn parse(value: &str) -> Decimal18 {
    value.parse::<Decimal18>().unwrap_or(Decimal18::ZERO)
}

/// Compare expected and broker positions within tolerance.
#[must_use]
pub fn reconcile_positions(
    expected: &[ExpectedPosition],
    actual: &[BrokerPosition],
    tolerances: ReconciliationTolerances,
) -> Vec<PositionDelta> {
    let mut expected_map: BTreeMap<&str, &str> = BTreeMap::new();
    for position in expected {
        expected_map.insert(position.symbol.as_str(), position.quantity.as_str());
    }
    let mut actual_map: BTreeMap<&str, &str> = BTreeMap::new();
    for position in actual {
        actual_map.insert(position.symbol.as_str(), position.quantity.as_str());
    }
    let mut symbols: Vec<&str> = expected_map
        .keys()
        .chain(actual_map.keys())
        .copied()
        .collect();
    symbols.sort_unstable();
    symbols.dedup();
    let zero = "0".to_string();
    let mut deltas = Vec::new();
    for symbol in symbols {
        let expected_quantity = expected_map.get(symbol).copied().unwrap_or(&zero);
        let actual_quantity = actual_map.get(symbol).copied().unwrap_or(&zero);
        let difference = parse(expected_quantity)
            .checked_sub(parse(actual_quantity))
            .unwrap_or(Decimal18::ZERO);
        if difference.abs().less_than(tolerances.position_abs)
            || difference.abs() == tolerances.position_abs
        {
            continue;
        }
        deltas.push(PositionDelta {
            symbol: symbol.to_string(),
            expected: expected_quantity.to_string(),
            actual: actual_quantity.to_string(),
            difference: difference.to_string(),
        });
    }
    deltas
}

/// Compare expected and broker cash within tolerance.
#[must_use]
pub fn reconcile_cash(
    expected: &str,
    actual: &str,
    tolerances: ReconciliationTolerances,
) -> Option<String> {
    let difference = parse(expected)
        .checked_sub(parse(actual))
        .unwrap_or(Decimal18::ZERO);
    if difference.abs().less_than(tolerances.cash_abs) || difference.abs() == tolerances.cash_abs {
        None
    } else {
        Some(difference.to_string())
    }
}

/// Deterministic periodic reconciliation cadence.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReconcileSchedule {
    /// Events between periodic passes.
    pub every_events: u64,
}

impl ReconcileSchedule {
    /// Create a schedule.
    ///
    /// # Errors
    ///
    /// Returns an error message when `every_events` is zero.
    pub fn new(every_events: u64) -> Result<Self, String> {
        if every_events == 0 {
            return Err("every_events must be positive".to_string());
        }
        Ok(Self { every_events })
    }

    /// Whether a pass is due after this many events.
    #[must_use]
    pub fn due(&self, market_event_count: u64) -> bool {
        market_event_count > 0 && market_event_count.is_multiple_of(self.every_events)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_match_reports_no_deltas() {
        let expected = vec![ExpectedPosition {
            symbol: "SPY".to_string(),
            quantity: "10".to_string(),
        }];
        let actual = vec![BrokerPosition {
            symbol: "SPY".to_string(),
            quantity: "10".to_string(),
        }];
        let deltas = reconcile_positions(&expected, &actual, ReconciliationTolerances::default());
        assert!(deltas.is_empty());
    }

    #[test]
    fn reports_signed_differences() {
        let expected = vec![ExpectedPosition {
            symbol: "SPY".to_string(),
            quantity: "10".to_string(),
        }];
        let actual = vec![BrokerPosition {
            symbol: "SPY".to_string(),
            quantity: "8".to_string(),
        }];
        let deltas = reconcile_positions(&expected, &actual, ReconciliationTolerances::default());
        assert_eq!(deltas.len(), 1);
        assert_eq!(deltas[0].difference, "2.000000000000000000");
    }

    #[test]
    fn tolerance_suppresses_small_differences() {
        let expected = vec![ExpectedPosition {
            symbol: "SPY".to_string(),
            quantity: "10.00".to_string(),
        }];
        let actual = vec![BrokerPosition {
            symbol: "SPY".to_string(),
            quantity: "10.000000000000000001".to_string(),
        }];
        let tolerances = ReconciliationTolerances {
            position_abs: Decimal18::from_unscaled(10).expect("tiny"),
            cash_abs: Decimal18::ZERO,
        };
        assert!(reconcile_positions(&expected, &actual, tolerances).is_empty());
    }

    #[test]
    fn schedule_cadence() {
        let schedule = ReconcileSchedule::new(4).expect("schedule");
        assert!(!schedule.due(0));
        assert!(schedule.due(4));
        assert!(!schedule.due(5));
    }
}
