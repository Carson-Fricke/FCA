//! The author-facing indicator protocol.
//!
//! Indicators declare exact inputs through the shared market-consumer contract,
//! own reusable nested dependencies, and expose their in-progress value.

use num::Decimal;

use crate::{MarketConsumer, ObservationContext};

/// A composable observation-only market consumer with an in-progress value.
pub trait Indicator: MarketConsumer<Context = ObservationContext> {
    /// The in-progress value as of the last accepted event, or `None`
    /// before warm-up.
    fn value(&self) -> Option<Decimal>;
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
pub fn indicator_add(left: Decimal, right: Decimal) -> Result<Decimal, num::DecimalError> {
    left.checked_add_with(right, &indicator_context())
}

/// Subtract two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the exact difference does not fit the context.
pub fn indicator_sub(left: Decimal, right: Decimal) -> Result<Decimal, num::DecimalError> {
    left.checked_sub_with(right, &indicator_context())
}

/// Multiply two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the exact product does not fit the context.
pub fn indicator_mul(left: Decimal, right: Decimal) -> Result<Decimal, num::DecimalError> {
    left.checked_mul_with(right, &indicator_context())
}

/// Divide two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the quotient cannot be represented.
pub fn indicator_div(left: Decimal, right: Decimal) -> Result<Decimal, num::DecimalError> {
    left.checked_div_with(right, &indicator_context())
}
