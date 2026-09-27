//! The author-facing indicator protocol.
//!
//! Indicators are tick-native and symbol-bound: they fold engine-dispatched
//! events in event time and expose the in-progress value. This is the SDK
//! authoring surface for the Python `TickIndicator` contract; the bucketing
//! and snapshot machinery is an implementation concern, not the authoring
//! seam.

use num::PyDecimal;

use crate::events::{QuoteEvent, TradeEvent};

/// One symbol-bound tick indicator.
pub trait Indicator {
    /// Fold one top-of-book quote.
    fn on_quote(&mut self, _event: &QuoteEvent) {}

    /// Fold one trade print.
    fn on_trade(&mut self, _event: &TradeEvent) {}

    /// The in-progress value as of the last accepted event, or `None`
    /// before warm-up.
    fn value(&self) -> Option<PyDecimal>;
}

/// The precision of the pinned indicator arithmetic context.
pub const INDICATOR_CONTEXT_PRECISION: u32 = num::INDICATOR_PRECISION;

/// The pinned indicator arithmetic context (34 significant digits).
#[must_use]
pub const fn indicator_context() -> num::Context {
    num::Context::new(num::INDICATOR_PRECISION, num::RoundingMode::HalfEven)
}

/// Add two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the exact sum does not fit the context.
pub fn indicator_add(left: PyDecimal, right: PyDecimal) -> Result<PyDecimal, num::DecimalError> {
    left.checked_add_with(right, &indicator_context())
}

/// Subtract two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the exact difference does not fit the context.
pub fn indicator_sub(left: PyDecimal, right: PyDecimal) -> Result<PyDecimal, num::DecimalError> {
    left.checked_sub_with(right, &indicator_context())
}

/// Multiply two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the exact product does not fit the context.
pub fn indicator_mul(left: PyDecimal, right: PyDecimal) -> Result<PyDecimal, num::DecimalError> {
    left.checked_mul_with(right, &indicator_context())
}

/// Divide two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the quotient cannot be represented.
pub fn indicator_div(left: PyDecimal, right: PyDecimal) -> Result<PyDecimal, num::DecimalError> {
    left.checked_div_with(right, &indicator_context())
}
