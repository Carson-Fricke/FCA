//! Pinned scale-18 fixed-point decimal kernel shared by the OMS, books, and
//! market modules.
//!
//! Unscaled values are `i128` at scale 18 (`decimal128(38, 18)`, the
//! repository's binary decimal convention); every operation is range-checked
//! against `decimal128(38, 18)` and never rounds on exact inputs. [`mul`] and
//! [`div`] round half-even at scale 18 when the exact result is not
//! representable.
//!
//! This kernel is the free-function view of the same semantics as the
//! [`num::Decimal18`] type and agrees with the pinned golden vectors.

use crate::wide::U256;

/// Number of fractional decimal digits carried by the kernel.
pub const SCALE: u32 = 18;

/// `10^18` unscaled units per whole unit.
pub const ONE: i128 = 1_000_000_000_000_000_000;

/// Largest magnitude representable by `decimal128(38, 18)`: `10^38 - 1`.
pub const MAX_UNSCALED: i128 = 99_999_999_999_999_999_999_999_999_999_999_999_999;

/// A checked failure from a decimal operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DecimalError {
    /// Fixed-point overflow or a non-representable value.
    OutOfRange,
    /// Division by zero.
    InvalidDecimal,
}

impl core::fmt::Display for DecimalError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::OutOfRange => "fixed-point result is outside decimal128(38, 18)",
            Self::InvalidDecimal => "invalid decimal operation",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for DecimalError {}

/// Scale a whole `i64` into the kernel representation.
pub const fn from_int(value: i64) -> i128 {
    (value as i128) * ONE
}

/// Range-check a raw unscaled value against `decimal128(38, 18)`.
fn narrow(value: i128) -> Result<i128, DecimalError> {
    if !(-MAX_UNSCALED..=MAX_UNSCALED).contains(&value) {
        return Err(DecimalError::OutOfRange);
    }
    Ok(value)
}

/// Narrow a 256-bit magnitude plus sign into a scale-18 value.
fn narrow_wide(magnitude: U256, negative: bool) -> Result<i128, DecimalError> {
    if magnitude.high != 0 || magnitude.low > MAX_UNSCALED as u128 {
        return Err(DecimalError::OutOfRange);
    }
    let value = magnitude.low as i128;
    Ok(if negative { -value } else { value })
}

/// Add two scale-18 values with range checking.
pub fn add(a: i128, b: i128) -> Result<i128, DecimalError> {
    let sum = a.checked_add(b).ok_or(DecimalError::OutOfRange)?;
    narrow(sum)
}

/// Subtract two scale-18 values with range checking.
pub fn sub(a: i128, b: i128) -> Result<i128, DecimalError> {
    let difference = a.checked_sub(b).ok_or(DecimalError::OutOfRange)?;
    narrow(difference)
}

/// Multiply two scale-18 values; exact when the product has at most 18
/// fractional digits, otherwise rounded half-even at scale 18.
pub fn mul(a: i128, b: i128) -> Result<i128, DecimalError> {
    let negative = (a < 0) != (b < 0);
    let numerator = U256::mul_u128(a.unsigned_abs(), b.unsigned_abs());
    let quotient = div_round_half_even(numerator, U256::from_u128(ONE as u128));
    narrow_wide(quotient, negative)
}

/// Compute `a * b / c` with a single half-even rounding at scale 18, avoiding
/// a double rounding between the multiply and the divide.
pub fn mul_div(a: i128, b: i128, c: i128) -> Result<i128, DecimalError> {
    if c == 0 {
        return Err(DecimalError::InvalidDecimal);
    }
    let negative = ((a < 0) != (b < 0)) != (c < 0);
    let numerator = U256::mul_u128(a.unsigned_abs(), b.unsigned_abs());
    let quotient = div_round_half_even(numerator, U256::from_u128(c.unsigned_abs()));
    narrow_wide(quotient, negative)
}

/// Divide `a` by `b` at scale 18, rounding half-even.
pub fn div(a: i128, b: i128) -> Result<i128, DecimalError> {
    if b == 0 {
        return Err(DecimalError::InvalidDecimal);
    }
    let negative = (a < 0) != (b < 0);
    let numerator = U256::mul_u128(a.unsigned_abs(), ONE as u128);
    let quotient = div_round_half_even(numerator, U256::from_u128(b.unsigned_abs()));
    narrow_wide(quotient, negative)
}

/// Divide two magnitudes with half-even rounding (magnitudes are unsigned).
fn div_round_half_even(numerator: U256, denominator: U256) -> U256 {
    let (quotient, remainder) = U256::div_rem(numerator, denominator);
    if remainder.is_zero() {
        return quotient;
    }
    let twice = remainder.shl1();
    if twice > denominator || (twice == denominator && quotient.bit(0)) {
        // The true quotient is below `2^128` for every input the kernel
        // accepts, so the increment cannot overflow.
        quotient.checked_add(U256::from_u128(1)).unwrap_or(quotient)
    } else {
        quotient
    }
}
