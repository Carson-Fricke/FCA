//! State reconstruction and break detection for paper/live parity.
//!
//! Deterministic core of `harness.runtime.reconciler`: tolerance-bounded
//! comparison of expected position and cash state against a broker snapshot,
//! plus the cadence at which reconciliation is due. Broker I/O stays behind
//! [`super::paper::PaperShell`] and the object-store ports.

use std::collections::BTreeMap;

use num::Decimal18;
use num::DecimalError;

use harness_trading::replay_errors::BacktestError;

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

/// Parse one string-exact broker or expected quantity or cash value.
///
/// # Errors
///
/// Returns [`BacktestError::DataPreflight`] when the text is not an exact
/// decimal. A malformed value is never read as zero.
fn parse(field: &str, value: &str) -> Result<Decimal18, BacktestError> {
    value.parse::<Decimal18>().map_err(|error| {
        BacktestError::DataPreflight(format!(
            "{field} {value:?} is not an exact decimal: {error}"
        ))
    })
}

fn arithmetic(operation: &str, error: DecimalError) -> BacktestError {
    BacktestError::Arithmetic(format!(
        "{operation} cannot be represented exactly: {error}"
    ))
}

/// Compare expected and broker positions within tolerance.
///
/// A symbol absent from one side is a flat position, which is a valid zero. A
/// quantity that is not an exact decimal is never read as zero.
///
/// # Errors
///
/// Returns [`BacktestError::DataPreflight`] when a position quantity is not an
/// exact decimal, and [`BacktestError::Arithmetic`] when a difference leaves the
/// decimal128(38, 18) bound.
pub fn reconcile_positions(
    expected: &[ExpectedPosition],
    actual: &[BrokerPosition],
    tolerances: ReconciliationTolerances,
) -> Result<Vec<PositionDelta>, BacktestError> {
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
        let difference = parse(
            &format!("expected quantity for {symbol}"),
            expected_quantity,
        )?
        .checked_sub(parse(
            &format!("broker quantity for {symbol}"),
            actual_quantity,
        )?)
        .map_err(|error| arithmetic("position difference", error))?;
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
    Ok(deltas)
}

/// Compare expected and broker cash within tolerance.
///
/// # Errors
///
/// Returns [`BacktestError::DataPreflight`] when either cash value is not an
/// exact decimal, and [`BacktestError::Arithmetic`] when the difference leaves
/// the decimal128(38, 18) bound.
pub fn reconcile_cash(
    expected: &str,
    actual: &str,
    tolerances: ReconciliationTolerances,
) -> Result<Option<String>, BacktestError> {
    let difference = parse("expected cash", expected)?
        .checked_sub(parse("broker cash", actual)?)
        .map_err(|error| arithmetic("cash difference", error))?;
    if difference.abs().less_than(tolerances.cash_abs) || difference.abs() == tolerances.cash_abs {
        Ok(None)
    } else {
        Ok(Some(difference.to_string()))
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
