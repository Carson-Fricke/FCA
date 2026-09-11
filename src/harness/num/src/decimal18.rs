//! Scale-18 fixed-point decimal core.
//!
//! This is the Rust port of the pinned native kernel in
//! `src/harness/native/decimal.zig`: an `i128` unscaled value at scale 18,
//! matching the repository's binary decimal convention `decimal128(38, 18)`.
//! Exact inputs never round; `mul` and `div` round the exact result to scale 18
//! using half-even by default, and every operation is range-checked against
//! `decimal128(38, 18)`.
//!
//! All arithmetic uses a stack-resident 256-bit intermediate
//! ([`crate::wide::U256`]); no heap allocation happens on an arithmetic path.

use core::fmt;
use core::ops::{Add, AddAssign, Div, DivAssign, Mul, MulAssign, Neg, Sub, SubAssign};
use core::str::FromStr;

use crate::wide::U256;

/// Number of fractional decimal digits carried by [`Decimal18`].
pub const SCALE: u32 = 18;

/// Unscaled units per whole unit: `10^18`.
pub const ONE_UNSCALED: i128 = 1_000_000_000_000_000_000;

/// Largest magnitude representable by `decimal128(38, 18)`: `10^38 - 1`.
pub const MAX_UNSCALED: i128 = 99_999_999_999_999_999_999_999_999_999_999_999_999;

/// Smallest (most negative) unscaled value representable by `decimal128(38, 18)`.
pub const MIN_UNSCALED: i128 = -MAX_UNSCALED;

/// A checked failure from a [`Decimal18`] operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DecimalError {
    /// The exact result is outside `decimal128(38, 18)`.
    Overflow,
    /// The divisor was zero.
    DivisionByZero,
    /// The input text is not a finite decimal literal.
    InvalidText,
    /// The input float was NaN or infinite.
    NonFinite,
    /// The requested rounding precision is outside `0..=SCALE`.
    InvalidScale,
    /// A context operation (for example `PyDecimal::quantize`) would produce a
    /// result the context cannot represent. Mirrors Python's
    /// `decimal.InvalidOperation`.
    InvalidOperation,
}

impl fmt::Display for DecimalError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::Overflow => "fixed-point result is outside decimal128(38, 18)",
            Self::DivisionByZero => "fixed-point division by zero",
            Self::InvalidText => "invalid decimal literal",
            Self::NonFinite => "non-finite decimal input",
            Self::InvalidScale => "invalid decimal scale",
            Self::InvalidOperation => "decimal operation is invalid for the context",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for DecimalError {}

/// Rounding behavior applied when an exact result is not representable at
/// scale 18 (or when rounding to fewer decimal places).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RoundingMode {
    /// Round half to even (banker's rounding).
    HalfEven,
    /// Round half away from zero.
    HalfUp,
    /// Round half toward zero.
    HalfDown,
    /// Discard the fractional part (round toward zero).
    Truncate,
    /// Round toward negative infinity.
    Floor,
    /// Round toward positive infinity.
    Ceiling,
}

impl RoundingMode {
    /// Parse one of the canonical mode names: `half_even`, `half_up`,
    /// `half_down`, `truncate`, `floor`, or `ceiling`.
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "half_even" => Some(Self::HalfEven),
            "half_up" => Some(Self::HalfUp),
            "half_down" => Some(Self::HalfDown),
            "truncate" => Some(Self::Truncate),
            "floor" => Some(Self::Floor),
            "ceiling" => Some(Self::Ceiling),
            _ => None,
        }
    }
}

/// An `i128` unscaled value at scale 18.
///
/// Every value is range-checked against `decimal128(38, 18)` on construction,
/// so `Decimal18` can never hold a value the native kernel could not.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Decimal18(i128);

impl Decimal18 {
    /// `0`.
    pub const ZERO: Self = Self(0);
    /// `1` as a scale-18 value.
    pub const ONE: Self = Self(ONE_UNSCALED);
    /// The largest representable value.
    pub const MAX: Self = Self(MAX_UNSCALED);
    /// The smallest representable value.
    pub const MIN: Self = Self(MIN_UNSCALED);

    /// Build from an unscaled integer, rejecting values outside
    /// `decimal128(38, 18)`.
    pub fn from_unscaled(unscaled: i128) -> Result<Self, DecimalError> {
        if !(MIN_UNSCALED..=MAX_UNSCALED).contains(&unscaled) {
            return Err(DecimalError::Overflow);
        }
        Ok(Self(unscaled))
    }

    /// The raw unscaled integer.
    pub const fn unscaled(self) -> i128 {
        self.0
    }

    /// True when the value is exactly zero.
    pub const fn is_zero(self) -> bool {
        self.0 == 0
    }

    /// True when the value is strictly negative.
    pub const fn is_negative(self) -> bool {
        self.0 < 0
    }

    /// True when the value is strictly positive.
    pub const fn is_positive(self) -> bool {
        self.0 > 0
    }

    /// `-1`, `0`, or `1` for negative, zero, and positive values.
    pub const fn signum(self) -> i32 {
        if self.0 < 0 {
            -1
        } else if self.0 > 0 {
            1
        } else {
            0
        }
    }

    /// True when `self < other`.
    pub const fn less_than(self, other: Self) -> bool {
        self.0 < other.0
    }

    /// The absolute value.
    pub fn abs(self) -> Self {
        // `self.0` is bounded by `MAX_UNSCALED`, so negation never overflows.
        Self(self.0.unsigned_abs() as i128)
    }

    /// Negation; always representable because the domain is symmetric.
    pub const fn checked_neg(self) -> Result<Self, DecimalError> {
        Ok(Self(-self.0))
    }

    /// Convert a whole number by scaling it up.
    pub fn from_i128(value: i128) -> Result<Self, DecimalError> {
        let negative = value < 0;
        let product = U256::mul_u128(value.unsigned_abs(), ONE_UNSCALED as u128);
        Self::from_wide_magnitude(product, negative)
    }

    /// Convert a whole `i64` by scaling it up (always representable).
    pub const fn from_i64(value: i64) -> Self {
        Self((value as i128) * ONE_UNSCALED)
    }

    /// Convert an `i128`-backed value to a whole number, applying `mode`.
    pub fn to_i128(self, mode: RoundingMode) -> Result<i128, DecimalError> {
        let negative = self.0 < 0;
        let rounded = round_magnitude(self.0.unsigned_abs(), ONE_UNSCALED as u128, negative, mode)?;
        // The rounded quotient is at most ceil(MAX_UNSCALED / 10^18) ~ 1e20.
        Ok(if negative {
            -(rounded as i128)
        } else {
            rounded as i128
        })
    }

    /// Convert to a whole `i64`, rejecting values that do not fit.
    pub fn to_i64(self, mode: RoundingMode) -> Result<i64, DecimalError> {
        i64::try_from(self.to_i128(mode)?).map_err(|_| DecimalError::Overflow)
    }

    /// Convert an exact `f64` by rounding half-even to scale 18.
    ///
    /// Floats cannot represent every scale-18 value; this conversion is the
    /// documented lossy bridge for float-only call sites.
    pub fn from_f64(value: f64) -> Result<Self, DecimalError> {
        if !value.is_finite() {
            return Err(DecimalError::NonFinite);
        }
        let scaled = value * ONE_UNSCALED as f64;
        if !scaled.is_finite() {
            return Err(DecimalError::Overflow);
        }
        let rounded = scaled.round_ties_even();
        if rounded > MAX_UNSCALED as f64 || rounded < MIN_UNSCALED as f64 {
            return Err(DecimalError::Overflow);
        }
        Ok(Self(rounded as i128))
    }

    /// Convert to the nearest `f64` (lossy).
    pub fn to_f64(self) -> f64 {
        self.0 as f64 / ONE_UNSCALED as f64
    }

    /// Checked addition, range-checked against `decimal128(38, 18)`.
    pub fn checked_add(self, rhs: Self) -> Result<Self, DecimalError> {
        let sum = self.0.checked_add(rhs.0).ok_or(DecimalError::Overflow)?;
        Self::from_unscaled(sum)
    }

    /// Checked subtraction, range-checked against `decimal128(38, 18)`.
    pub fn checked_sub(self, rhs: Self) -> Result<Self, DecimalError> {
        let difference = self.0.checked_sub(rhs.0).ok_or(DecimalError::Overflow)?;
        Self::from_unscaled(difference)
    }

    /// Checked multiplication, rounded half-even at scale 18.
    pub fn checked_mul(self, rhs: Self) -> Result<Self, DecimalError> {
        let negative = (self.0 < 0) != (rhs.0 < 0);
        let numerator = U256::mul_u128(self.0.unsigned_abs(), rhs.0.unsigned_abs());
        let quotient = div_round(
            numerator,
            U256::from_u128(ONE_UNSCALED as u128),
            negative,
            RoundingMode::HalfEven,
        )?;
        Self::from_wide_magnitude(quotient, negative)
    }

    /// Checked division, rounded half-even at scale 18.
    pub fn checked_div(self, rhs: Self) -> Result<Self, DecimalError> {
        self.checked_div_with(rhs, RoundingMode::HalfEven)
    }

    /// Checked division with an explicit rounding mode.
    pub fn checked_div_with(self, rhs: Self, mode: RoundingMode) -> Result<Self, DecimalError> {
        if rhs.0 == 0 {
            return Err(DecimalError::DivisionByZero);
        }
        let negative = (self.0 < 0) != (rhs.0 < 0);
        let numerator = U256::mul_u128(self.0.unsigned_abs(), ONE_UNSCALED as u128);
        let quotient = div_round(
            numerator,
            U256::from_u128(rhs.0.unsigned_abs()),
            negative,
            mode,
        )?;
        Self::from_wide_magnitude(quotient, negative)
    }

    /// Compute `self * factor / divisor` with a single half-even rounding,
    /// avoiding a double rounding between the multiply and the divide.
    pub fn checked_mul_div(self, factor: Self, divisor: Self) -> Result<Self, DecimalError> {
        self.checked_mul_div_with(factor, divisor, RoundingMode::HalfEven)
    }

    /// Compute `self * factor / divisor` with an explicit rounding mode.
    pub fn checked_mul_div_with(
        self,
        factor: Self,
        divisor: Self,
        mode: RoundingMode,
    ) -> Result<Self, DecimalError> {
        if divisor.0 == 0 {
            return Err(DecimalError::DivisionByZero);
        }
        let negative = ((self.0 < 0) != (factor.0 < 0)) != (divisor.0 < 0);
        let numerator = U256::mul_u128(self.0.unsigned_abs(), factor.0.unsigned_abs());
        let quotient = div_round(
            numerator,
            U256::from_u128(divisor.0.unsigned_abs()),
            negative,
            mode,
        )?;
        Self::from_wide_magnitude(quotient, negative)
    }

    /// Round to `decimal_places` fractional digits using `mode`.
    ///
    /// `decimal_places` may be anything in `0..=SCALE`.
    pub fn round_to(self, mode: RoundingMode, decimal_places: u32) -> Result<Self, DecimalError> {
        if decimal_places > SCALE {
            return Err(DecimalError::InvalidScale);
        }
        let drop_digits = SCALE - decimal_places;
        let factor = pow10_u128(drop_digits);
        let negative = self.0 < 0;
        let rounded = round_magnitude(self.0.unsigned_abs(), factor, negative, mode)?;
        let scaled = rounded.checked_mul(factor).ok_or(DecimalError::Overflow)?;
        if scaled > MAX_UNSCALED as u128 {
            return Err(DecimalError::Overflow);
        }
        let value = scaled as i128;
        Ok(Self(if negative { -value } else { value }))
    }

    /// Build from a 256-bit magnitude plus sign, range-checking the result.
    fn from_wide_magnitude(magnitude: U256, negative: bool) -> Result<Self, DecimalError> {
        if magnitude.high != 0 {
            return Err(DecimalError::Overflow);
        }
        let value = magnitude.low;
        if value > MAX_UNSCALED as u128 {
            return Err(DecimalError::Overflow);
        }
        let value = value as i128;
        Ok(Self(if negative { -value } else { value }))
    }
}

/// Divide two 256-bit magnitudes with the requested rounding.
fn div_round(
    numerator: U256,
    denominator: U256,
    negative: bool,
    mode: RoundingMode,
) -> Result<U256, DecimalError> {
    let (quotient, remainder) = U256::div_rem(numerator, denominator);
    if remainder.is_zero() {
        return Ok(quotient);
    }
    let twice = remainder.shl1();
    let increment = match mode {
        RoundingMode::Truncate => false,
        RoundingMode::Floor => negative,
        RoundingMode::Ceiling => !negative,
        RoundingMode::HalfEven => twice > denominator || (twice == denominator && quotient.bit(0)),
        RoundingMode::HalfUp => twice >= denominator,
        RoundingMode::HalfDown => twice > denominator,
    };
    if increment {
        quotient
            .checked_add(U256::from_u128(1))
            .ok_or(DecimalError::Overflow)
    } else {
        Ok(quotient)
    }
}

/// Divide a `u128` magnitude with the requested rounding, returning the
/// rounded whole-number quotient. Used by integer conversion, `round_to`, and
/// text parsing. `divisor` must be non-zero.
fn round_magnitude(
    magnitude: u128,
    divisor: u128,
    negative: bool,
    mode: RoundingMode,
) -> Result<u128, DecimalError> {
    let quotient = magnitude / divisor;
    let remainder = magnitude % divisor;
    if remainder == 0 {
        return Ok(quotient);
    }
    let twice = remainder * 2;
    let increment = match mode {
        RoundingMode::Truncate => false,
        RoundingMode::Floor => negative,
        RoundingMode::Ceiling => !negative,
        RoundingMode::HalfEven => twice > divisor || (twice == divisor && quotient & 1 == 1),
        RoundingMode::HalfUp => twice >= divisor,
        RoundingMode::HalfDown => twice > divisor,
    };
    if increment {
        quotient.checked_add(1).ok_or(DecimalError::Overflow)
    } else {
        Ok(quotient)
    }
}

/// `10^exponent` for `exponent <= 38`.
const fn pow10_u128(exponent: u32) -> u128 {
    let mut result = 1u128;
    let mut step = 0u32;
    while step < exponent {
        result *= 10;
        step += 1;
    }
    result
}

impl FromStr for Decimal18 {
    type Err = DecimalError;

    /// Parse a finite decimal literal, rounding half-even at scale 18 when the
    /// literal has more than 18 fractional digits.
    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let text = text.trim();
        if text.is_empty() {
            return Err(DecimalError::InvalidText);
        }
        let bytes = text.as_bytes();
        let mut index = 0usize;
        let negative = match bytes[0] {
            b'+' => {
                index = 1;
                false
            }
            b'-' => {
                index = 1;
                true
            }
            _ => false,
        };
        if index == bytes.len() {
            return Err(DecimalError::InvalidText);
        }

        let mut mantissa: i128 = 0;
        let mut fraction_digits: i64 = 0;
        let mut exponent: i64 = 0;
        let mut seen_digit = false;
        let mut seen_point = false;
        let mut seen_exponent = false;

        while index < bytes.len() {
            let byte = bytes[index];
            match byte {
                b'0'..=b'9' => {
                    if seen_exponent {
                        return Err(DecimalError::InvalidText);
                    }
                    seen_digit = true;
                    let digit = i128::from(byte - b'0');
                    mantissa = mantissa
                        .checked_mul(10)
                        .and_then(|value| value.checked_add(digit))
                        .ok_or(DecimalError::Overflow)?;
                    if seen_point {
                        fraction_digits += 1;
                    }
                    index += 1;
                }
                b'.' if !seen_point && !seen_exponent => {
                    seen_point = true;
                    index += 1;
                }
                b'e' | b'E' if !seen_exponent => {
                    seen_exponent = true;
                    index += 1;
                    let exponent_negative = match bytes.get(index) {
                        Some(b'+') => {
                            index += 1;
                            false
                        }
                        Some(b'-') => {
                            index += 1;
                            true
                        }
                        _ => false,
                    };
                    let start = index;
                    let mut value: i64 = 0;
                    while index < bytes.len() && bytes[index].is_ascii_digit() {
                        value = value
                            .checked_mul(10)
                            .and_then(|current| current.checked_add(i64::from(bytes[index] - b'0')))
                            .unwrap_or(i64::MAX / 2);
                        index += 1;
                    }
                    if index == start {
                        return Err(DecimalError::InvalidText);
                    }
                    exponent = if exponent_negative { -value } else { value };
                }
                _ => return Err(DecimalError::InvalidText),
            }
        }
        if !seen_digit {
            return Err(DecimalError::InvalidText);
        }
        Self::from_mantissa(mantissa, exponent, fraction_digits, negative)
    }
}

impl Decimal18 {
    /// Assemble `mantissa * 10^(exponent - fraction_digits)` at scale 18.
    fn from_mantissa(
        mantissa: i128,
        exponent: i64,
        fraction_digits: i64,
        negative: bool,
    ) -> Result<Self, DecimalError> {
        if mantissa == 0 {
            return Ok(Self::ZERO);
        }
        let power = i64::from(SCALE) + exponent - fraction_digits;
        if power >= 0 {
            let power = u32::try_from(power).map_err(|_| DecimalError::Overflow)?;
            if power > 38 {
                return Err(DecimalError::Overflow);
            }
            let product = U256::mul_u128(mantissa.unsigned_abs(), pow10_u128(power));
            return Self::from_wide_magnitude(product, negative);
        }

        let drop_digits = power.unsigned_abs();
        let magnitude = mantissa.unsigned_abs();
        if drop_digits > 38 {
            // `10^drop_digits` exceeds `u128` and is more than twice the largest
            // possible magnitude, so half-even rounding always lands on zero.
            return Ok(Self::ZERO);
        }
        let divisor = pow10_u128(u32::try_from(drop_digits).map_err(|_| DecimalError::Overflow)?);
        let rounded = round_magnitude(magnitude, divisor, negative, RoundingMode::HalfEven)?;
        let value = rounded as i128;
        Ok(Self(if negative { -value } else { value }))
    }
}

impl fmt::Display for Decimal18 {
    /// Render the canonical decimal text, byte-identical to Python's
    /// `str(Decimal(unscaled).scaleb(-scale))`.
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let negative = self.0 < 0;
        let mut buffer = [0u8; 39];
        let count = write_digits(self.0.unsigned_abs(), &mut buffer);
        let digits = &buffer[..count];
        if negative {
            write_ascii(formatter, b"-")?;
        }
        let adjusted = -(i64::from(SCALE)) + count as i64 - 1;
        if adjusted >= -6 {
            if count > SCALE as usize {
                let point = count - SCALE as usize;
                write_ascii(formatter, &digits[..point])?;
                write_ascii(formatter, b".")?;
                write_ascii(formatter, &digits[point..])?;
            } else {
                write_ascii(formatter, b"0.")?;
                for _ in 0..(SCALE as usize - count) {
                    write_ascii(formatter, b"0")?;
                }
                write_ascii(formatter, digits)?;
            }
        } else {
            write_ascii(formatter, &digits[..1])?;
            if count > 1 {
                write_ascii(formatter, b".")?;
                write_ascii(formatter, &digits[1..])?;
            }
            write_ascii(formatter, b"E")?;
            if adjusted >= 0 {
                write_ascii(formatter, b"+")?;
            }
            write!(formatter, "{adjusted}")?;
        }
        Ok(())
    }
}

/// Write the decimal digits of `magnitude` big-endian into `buffer`,
/// returning the digit count. Zero renders as a single `0`.
fn write_digits(mut magnitude: u128, buffer: &mut [u8; 39]) -> usize {
    if magnitude == 0 {
        buffer[0] = b'0';
        return 1;
    }
    let mut index = buffer.len();
    while magnitude > 0 {
        index -= 1;
        buffer[index] = b'0' + (magnitude % 10) as u8;
        magnitude /= 10;
    }
    let count = buffer.len() - index;
    buffer.copy_within(index.., 0);
    count
}

/// Write an ASCII byte slice without requiring a `&str`.
fn write_ascii(formatter: &mut fmt::Formatter<'_>, bytes: &[u8]) -> fmt::Result {
    let text = core::str::from_utf8(bytes).map_err(|_| fmt::Error)?;
    formatter.write_str(text)
}

impl Add for Decimal18 {
    type Output = Self;
    fn add(self, rhs: Self) -> Self {
        self.checked_add(rhs).expect("Decimal18 addition overflow")
    }
}

impl Sub for Decimal18 {
    type Output = Self;
    fn sub(self, rhs: Self) -> Self {
        self.checked_sub(rhs)
            .expect("Decimal18 subtraction overflow")
    }
}

impl Mul for Decimal18 {
    type Output = Self;
    fn mul(self, rhs: Self) -> Self {
        self.checked_mul(rhs)
            .expect("Decimal18 multiplication overflow")
    }
}

impl Div for Decimal18 {
    type Output = Self;
    fn div(self, rhs: Self) -> Self {
        self.checked_div(rhs).expect("Decimal18 division failed")
    }
}

impl Neg for Decimal18 {
    type Output = Self;
    fn neg(self) -> Self {
        Self(-self.0)
    }
}

impl AddAssign for Decimal18 {
    fn add_assign(&mut self, rhs: Self) {
        *self = *self + rhs;
    }
}

impl SubAssign for Decimal18 {
    fn sub_assign(&mut self, rhs: Self) {
        *self = *self - rhs;
    }
}

impl MulAssign for Decimal18 {
    fn mul_assign(&mut self, rhs: Self) {
        *self = *self * rhs;
    }
}

impl DivAssign for Decimal18 {
    fn div_assign(&mut self, rhs: Self) {
        *self = *self / rhs;
    }
}

impl From<i64> for Decimal18 {
    fn from(value: i64) -> Self {
        Self::from_i64(value)
    }
}

impl From<u64> for Decimal18 {
    fn from(value: u64) -> Self {
        Self((value as i128) * ONE_UNSCALED)
    }
}

impl From<i32> for Decimal18 {
    fn from(value: i32) -> Self {
        Self::from_i64(i64::from(value))
    }
}

impl From<u32> for Decimal18 {
    fn from(value: u32) -> Self {
        Self(i128::from(value) * ONE_UNSCALED)
    }
}

impl TryFrom<i128> for Decimal18 {
    type Error = DecimalError;
    fn try_from(value: i128) -> Result<Self, Self::Error> {
        Self::from_i128(value)
    }
}

impl TryFrom<f64> for Decimal18 {
    type Error = DecimalError;
    fn try_from(value: f64) -> Result<Self, Self::Error> {
        Self::from_f64(value)
    }
}

#[cfg(test)]
#[path = "../tests/unit/decimal18.rs"]
mod tests;
