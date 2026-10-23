//! Variable-scale exact decimal.
//!
//! [`Decimal`] preserves the *scale* of every value: the exponent, the
//! coefficient, and its trailing zeros are all observable through `Display`, so
//! the engine's canonical byte text survives a round trip. The fixed-scale
//! [`crate::Decimal18`] kernel deliberately discards that information, so
//! byte-identical artifact replay needs this variable-scale type.
//!
//! [`Decimal`] stores a magnitude coefficient (`u128`), a sign flag, and a
//! decimal scale (`i32`), so the value is
//! `(-1)^negative * coefficient * 10^-scale`, including `-0` and trailing-zero
//! preservation. Parsing keeps the input scale (for example `"1.500"` stays
//! `1.500` and `"1E+2"` stays `1E+2`), and `Display` reproduces the engine's
//! canonical decimal text byte for byte.
//!
//! Arithmetic follows the context rules used by the engine: `+`, `-`, `*`,
//! and `/` round the exact result to the context precision (28 significant
//! digits, half-even by default), while `+`/`-` take the maximum operand
//! scale, `*` the summed scales, and `/` the correctly-rounded quotient with
//! the engine's ideal-exponent behavior. [`Context`] allows the pinned
//! 34-digit indicator context to be selected.
//!
//! Non-finite values (NaN and infinities) are rejected on input, matching the
//! engine's exact-decimal contract; the type only ever holds finite values.
//!
//! [`Decimal::to_f64`] is the one-way boundary to analytic math: exact decimals
//! stay exact for data, orders, fills, fees, and cash, while metrics and
//! statistics use correctly-rounded `f64`.

use core::cmp::Ordering;
use core::fmt;
use core::hash::Hash;
use core::hash::Hasher;
use core::ops::Add;
use core::ops::AddAssign;
use core::ops::Div;
use core::ops::DivAssign;
use core::ops::Mul;
use core::ops::MulAssign;
use core::ops::Neg;
use core::ops::Sub;
use core::ops::SubAssign;
use core::str::FromStr;

use crate::decimal18::DecimalError;
use crate::decimal18::RoundingMode;
use crate::wide::U256;

/// The default context precision (28 significant digits).
pub const DEFAULT_PRECISION: u32 = 28;

/// The precision pinned by the indicator arithmetic context.
pub const INDICATOR_PRECISION: u32 = 34;

/// A pinned arithmetic context: precision in significant digits and the
/// rounding mode applied when an exact result does not fit.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Context {
    precision: u32,
    rounding: RoundingMode,
}

impl Context {
    /// A new context. `precision` is clamped to at least one digit.
    pub const fn new(precision: u32, rounding: RoundingMode) -> Self {
        let precision = if precision == 0 { 1 } else { precision };
        Self {
            precision,
            rounding,
        }
    }

    /// The context precision in significant digits.
    pub const fn precision(self) -> u32 {
        self.precision
    }

    /// The rounding mode.
    pub const fn rounding(self) -> RoundingMode {
        self.rounding
    }
}

impl Default for Context {
    fn default() -> Self {
        Self::new(DEFAULT_PRECISION, RoundingMode::HalfEven)
    }
}

/// A finite, variable-scale decimal value.
#[derive(Clone, Copy, Debug, Default)]
pub struct Decimal {
    negative: bool,
    coefficient: u128,
    scale: i32,
}

impl Decimal {
    /// Positive zero at scale zero.
    pub const ZERO: Self = Self {
        negative: false,
        coefficient: 0,
        scale: 0,
    };

    /// One at scale zero.
    pub const ONE: Self = Self {
        negative: false,
        coefficient: 1,
        scale: 0,
    };

    /// Build directly from a sign, magnitude coefficient, and scale.
    pub const fn from_parts(negative: bool, coefficient: u128, scale: i32) -> Self {
        Self {
            negative,
            coefficient,
            scale,
        }
    }

    /// The magnitude coefficient (the decimal digits as an integer).
    pub const fn coefficient(self) -> u128 {
        self.coefficient
    }

    /// The decimal scale; the value is `coefficient * 10^-scale`.
    pub const fn scale(self) -> i32 {
        self.scale
    }

    /// The decimal exponent (`-scale`).
    pub const fn exponent(self) -> i32 {
        -self.scale
    }

    /// True when the value is numerically zero.
    pub const fn is_zero(self) -> bool {
        self.coefficient == 0
    }

    /// True when the sign flag is set, including for `-0`.
    pub const fn is_signed(self) -> bool {
        self.negative
    }

    /// True when the value is numerically negative.
    pub const fn is_negative(self) -> bool {
        self.negative && self.coefficient != 0
    }

    /// True when the value is numerically positive.
    pub const fn is_positive(self) -> bool {
        !self.negative && self.coefficient != 0
    }

    /// `-1`, `0`, or `1` for negative, zero, and positive values.
    pub const fn signum(self) -> i32 {
        if self.coefficient == 0 {
            0
        } else if self.negative {
            -1
        } else {
            1
        }
    }

    /// The absolute value; clears the sign, including for `-0`.
    pub const fn abs(self) -> Self {
        Self {
            negative: false,
            ..self
        }
    }

    /// Negation; either zero maps to positive zero.
    pub const fn negated(self) -> Self {
        Self {
            negative: if self.coefficient == 0 {
                false
            } else {
                !self.negative
            },
            ..self
        }
    }

    /// Build a whole number. Every `i128` is representable.
    pub const fn from_i128(value: i128) -> Self {
        Self {
            negative: value < 0,
            coefficient: value.unsigned_abs(),
            scale: 0,
        }
    }

    /// Build from an unsigned magnitude at scale zero.
    pub const fn from_u128(value: u128) -> Self {
        Self {
            negative: false,
            coefficient: value,
            scale: 0,
        }
    }

    /// Multiply by `10^n` by shifting the exponent.
    pub fn scaleb(self, power: i32) -> Result<Self, DecimalError> {
        let scale = self
            .scale
            .checked_sub(power)
            .ok_or(DecimalError::Overflow)?;
        Ok(Self { scale, ..self })
    }

    /// Round to an integer using `context`'s rounding mode, keeping the
    /// no-precision-signal behavior.
    pub fn to_integral_value(self, context: &Context) -> Self {
        if self.scale <= 0 {
            return self;
        }
        let dropped = self.scale as u32;
        let quotient = match pow10_u128(dropped) {
            Some(divisor) => {
                round_div_u128(self.coefficient, divisor, self.negative, context.rounding)
            }
            // `10^dropped` exceeds the coefficient range, so the quotient is
            // zero or one depending only on the directed rounding mode.
            None => match context.rounding {
                RoundingMode::Floor if self.negative && self.coefficient != 0 => 1,
                RoundingMode::Ceiling if !self.negative && self.coefficient != 0 => 1,
                _ => 0,
            },
        };
        Self {
            negative: self.negative,
            coefficient: quotient,
            scale: 0,
        }
    }

    /// Truncate toward zero.
    pub fn trunc_i128(self) -> Result<i128, DecimalError> {
        let magnitude = if self.scale <= 0 {
            let shift = (-self.scale) as u32;
            let factor = pow10_u128(shift).ok_or(DecimalError::Overflow)?;
            self.coefficient
                .checked_mul(factor)
                .ok_or(DecimalError::Overflow)?
        } else {
            let divisor = pow10_u128(self.scale as u32).ok_or(DecimalError::Overflow)?;
            self.coefficient / divisor
        };
        signed_i128(self.negative, magnitude)
    }

    /// Convert to an integer using `context`'s rounding mode.
    pub fn to_i128(self, context: &Context) -> Result<i128, DecimalError> {
        let integral = self.to_integral_value(context);
        let magnitude = if integral.scale <= 0 {
            let shift = (-integral.scale) as u32;
            let factor = pow10_u128(shift).ok_or(DecimalError::Overflow)?;
            integral
                .coefficient
                .checked_mul(factor)
                .ok_or(DecimalError::Overflow)?
        } else {
            integral.coefficient
        };
        signed_i128(integral.negative, magnitude)
    }

    /// Convert to the nearest `f64`, rounding to nearest with ties to even.
    ///
    /// This is the one-way boundary for analytics and metrics: the exact
    /// canonical decimal text is parsed with IEEE 754 correct rounding, so
    /// equal values with different scales (for example `"1.5"` and `"1.500"`)
    /// convert identically. A magnitude outside the `f64` range saturates to
    /// the signed infinity, like `float(Decimal)` in the reference engine.
    /// Use [`Decimal::to_f64_checked`] where non-finite output must fail.
    #[must_use]
    pub fn to_f64(self) -> f64 {
        self.to_string().parse::<f64>().unwrap_or(if self.negative {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        })
    }

    /// Checked conversion to `f64` for metric and statistic boundaries.
    ///
    /// # Errors
    ///
    /// Returns [`DecimalError::Overflow`] when the magnitude exceeds the `f64`
    /// range and the conversion would produce a non-finite value. Finite
    /// underflow to zero is not an error.
    pub fn to_f64_checked(self) -> Result<f64, DecimalError> {
        let value = self.to_f64();
        if value.is_finite() {
            Ok(value)
        } else {
            Err(DecimalError::Overflow)
        }
    }

    /// Exact addition under `context`.
    pub fn checked_add_with(self, rhs: Self, context: &Context) -> Result<Self, DecimalError> {
        self.checked_add_sub(rhs, false, context)
    }

    /// Exact subtraction under `context`.
    pub fn checked_sub_with(self, rhs: Self, context: &Context) -> Result<Self, DecimalError> {
        self.checked_add_sub(rhs, true, context)
    }

    /// Addition under the default context.
    pub fn checked_add(self, rhs: Self) -> Result<Self, DecimalError> {
        self.checked_add_with(rhs, &Context::default())
    }

    /// Subtraction under the default context.
    pub fn checked_sub(self, rhs: Self) -> Result<Self, DecimalError> {
        self.checked_sub_with(rhs, &Context::default())
    }

    /// Multiplication under the default context.
    pub fn checked_mul(self, rhs: Self) -> Result<Self, DecimalError> {
        self.checked_mul_with(rhs, &Context::default())
    }

    /// Division under the default context.
    pub fn checked_div(self, rhs: Self) -> Result<Self, DecimalError> {
        self.checked_div_with(rhs, &Context::default())
    }

    /// Quantize under the default context.
    pub fn quantize(self, quantum: Self) -> Result<Self, DecimalError> {
        self.quantize_with(quantum, &Context::default())
    }

    /// Normalize under the default context.
    pub fn normalize(self) -> Result<Self, DecimalError> {
        self.normalize_with(&Context::default())
    }

    /// Exact multiplication under `context`.
    pub fn checked_mul_with(self, rhs: Self, context: &Context) -> Result<Self, DecimalError> {
        let negative = self.negative != rhs.negative;
        let scale = i64::from(self.scale) + i64::from(rhs.scale);
        let scale = i32::try_from(scale).map_err(|_| DecimalError::Overflow)?;
        let product = U256::mul_u128(self.coefficient, rhs.coefficient);
        round_significant(product, scale, negative, context)
    }

    /// Exact division under `context`.
    pub fn checked_div_with(self, rhs: Self, context: &Context) -> Result<Self, DecimalError> {
        if rhs.coefficient == 0 {
            return Err(DecimalError::DivisionByZero);
        }
        let negative = self.negative != rhs.negative;
        if self.coefficient == 0 {
            let scale = i64::from(self.scale) - i64::from(rhs.scale);
            let scale = i32::try_from(scale).map_err(|_| DecimalError::Overflow)?;
            return Ok(Self {
                negative,
                coefficient: 0,
                scale,
            });
        }

        let precision = context.precision.max(1) as usize;
        let (digits, adjusted_ratio, more, exact) =
            significant_digits(self.coefficient, rhs.coefficient, precision + 1)?;
        let exponent_shift = i64::from(rhs.scale) - i64::from(self.scale);
        let mut adjusted = i64::from(adjusted_ratio) + exponent_shift;

        if exact && digits.len() <= precision {
            let coefficient = digits_to_u128(&digits).ok_or(DecimalError::Overflow)?;
            let scale = digits.len() as i64 - 1 - adjusted;
            return finish(negative, coefficient, scale);
        }

        // `significant_digits` returned `precision + 1` digits when rounding is
        // required (either inexact, or exact but longer than the precision).
        let round_digit = digits[precision];
        let sticky = more;
        let last_kept = digits[precision - 1];
        let round_up = should_round_up(context.rounding, negative, round_digit, sticky, last_kept);
        let mut coefficient = digits_to_u128(&digits[..precision]).ok_or(DecimalError::Overflow)?;
        if round_up {
            coefficient += 1;
            if decimal_digits_u128(coefficient) > context.precision.max(1) {
                coefficient /= 10;
                adjusted += 1;
            }
        }
        let scale = precision as i64 - 1 - adjusted;
        finish(negative, coefficient, scale)
    }

    /// Round to the exponent of `quantum` under `context`, matching the
    /// engine's `quantize`. Returns [`DecimalError::InvalidOperation`] when the
    /// result would need more than the context precision.
    pub fn quantize_with(self, quantum: Self, context: &Context) -> Result<Self, DecimalError> {
        let target_scale = quantum.scale;
        let self_exponent = -i64::from(self.scale);
        let target_exponent = -i64::from(target_scale);
        let difference = self_exponent - target_exponent;

        let magnitude = if self.coefficient == 0 {
            U256::ZERO
        } else if difference >= 0 {
            let shift = u32::try_from(difference).map_err(|_| DecimalError::InvalidOperation)?;
            shift_coefficient(self.coefficient, shift)
                .map_err(|_| DecimalError::InvalidOperation)?
        } else {
            let divisor = pow10_u128(u32::try_from(-difference).unwrap_or(u32::MAX))
                .ok_or(DecimalError::InvalidOperation)?;
            U256::from_u128(round_div_u128(
                self.coefficient,
                divisor,
                self.negative,
                context.rounding,
            ))
        };

        let digits = u256_to_digits(magnitude);
        if digits.len() as u32 > context.precision.max(1) {
            return Err(DecimalError::InvalidOperation);
        }
        let coefficient = digits_to_u128(&digits).ok_or(DecimalError::InvalidOperation)?;
        Ok(Self {
            negative: self.negative,
            coefficient,
            scale: target_scale,
        })
    }

    /// Remove trailing zeros and canonicalize zero, matching the engine's
    /// `normalize` under `context`.
    pub fn normalize_with(self, context: &Context) -> Result<Self, DecimalError> {
        if self.coefficient == 0 {
            return Ok(Self {
                negative: self.negative,
                coefficient: 0,
                scale: 0,
            });
        }
        let rounded = round_significant(
            U256::from_u128(self.coefficient),
            self.scale,
            self.negative,
            context,
        )?;
        let mut coefficient = rounded.coefficient;
        let mut scale = rounded.scale;
        while coefficient.is_multiple_of(10) {
            coefficient /= 10;
            scale = scale.checked_sub(1).ok_or(DecimalError::Overflow)?;
        }
        Ok(Self {
            negative: rounded.negative,
            coefficient,
            scale,
        })
    }

    fn checked_add_sub(
        self,
        rhs: Self,
        subtract: bool,
        context: &Context,
    ) -> Result<Self, DecimalError> {
        let rhs_negative = rhs.negative != subtract;
        let common = self.scale.max(rhs.scale);
        let left_shift = (i64::from(common) - i64::from(self.scale)) as u32;
        let right_shift = (i64::from(common) - i64::from(rhs.scale)) as u32;
        let left = shift_coefficient(self.coefficient, left_shift)?;
        let right = shift_coefficient(rhs.coefficient, right_shift)?;

        let (negative, magnitude) = if self.negative == rhs_negative {
            let sum = left.checked_add(right).ok_or(DecimalError::Overflow)?;
            (self.negative, sum)
        } else {
            match left.cmp(&right) {
                Ordering::Greater => (self.negative, left.wrapping_sub(right)),
                Ordering::Less => (rhs_negative, right.wrapping_sub(left)),
                Ordering::Equal => (self.negative && rhs_negative, U256::ZERO),
            }
        };

        if magnitude.is_zero() {
            return Ok(Self {
                negative,
                coefficient: 0,
                scale: common,
            });
        }
        round_significant(magnitude, common, negative, context)
    }

    /// Numeric comparison, `-0` equal to `0` and scale-independent.
    pub fn cmp_value(self, rhs: Self) -> Ordering {
        if self.coefficient == 0 && rhs.coefficient == 0 {
            return Ordering::Equal;
        }
        if self.coefficient == 0 {
            return if rhs.negative {
                Ordering::Greater
            } else {
                Ordering::Less
            };
        }
        if rhs.coefficient == 0 {
            return if self.negative {
                Ordering::Less
            } else {
                Ordering::Greater
            };
        }
        match (self.negative, rhs.negative) {
            (true, false) => Ordering::Less,
            (false, true) => Ordering::Greater,
            _ => {
                let ordering =
                    cmp_magnitudes(self.coefficient, self.scale, rhs.coefficient, rhs.scale);
                if self.negative {
                    ordering.reverse()
                } else {
                    ordering
                }
            }
        }
    }
}

/// Build a result from a coefficient and an exponent-derived scale.
fn finish(negative: bool, coefficient: u128, scale: i64) -> Result<Decimal, DecimalError> {
    let scale = i32::try_from(scale).map_err(|_| DecimalError::Overflow)?;
    Ok(Decimal {
        negative,
        coefficient,
        scale,
    })
}

/// Round an exact 256-bit magnitude to `context.precision` significant digits.
fn round_significant(
    magnitude: U256,
    scale: i32,
    negative: bool,
    context: &Context,
) -> Result<Decimal, DecimalError> {
    if magnitude.is_zero() {
        return Ok(Decimal {
            negative,
            coefficient: 0,
            scale,
        });
    }
    let digits = u256_to_digits(magnitude);
    let precision = context.precision.max(1) as usize;
    if digits.len() <= precision {
        let coefficient = digits_to_u128(&digits).ok_or(DecimalError::Overflow)?;
        return Ok(Decimal {
            negative,
            coefficient,
            scale,
        });
    }

    let drop = digits.len() - precision;
    let round_digit = digits[precision];
    let sticky = digits[precision + 1..].iter().any(|&digit| digit != 0);
    let last_kept = digits[precision - 1];
    let round_up = should_round_up(context.rounding, negative, round_digit, sticky, last_kept);
    let mut coefficient = digits_to_u128(&digits[..precision]).ok_or(DecimalError::Overflow)?;
    let mut scale = scale
        .checked_sub(drop as i32)
        .ok_or(DecimalError::Overflow)?;
    if round_up {
        coefficient += 1;
        if decimal_digits_u128(coefficient) > context.precision.max(1) {
            coefficient /= 10;
            scale = scale.checked_sub(1).ok_or(DecimalError::Overflow)?;
        }
    }
    Ok(Decimal {
        negative,
        coefficient,
        scale,
    })
}

/// Decide whether the discarded part rounds the magnitude up.
fn should_round_up(
    mode: RoundingMode,
    negative: bool,
    round_digit: u8,
    sticky: bool,
    last_kept: u8,
) -> bool {
    let discarded_nonzero = round_digit != 0 || sticky;
    match mode {
        RoundingMode::Truncate => false,
        RoundingMode::Floor => negative && discarded_nonzero,
        RoundingMode::Ceiling => !negative && discarded_nonzero,
        RoundingMode::HalfEven => {
            round_digit > 5 || (round_digit == 5 && (sticky || last_kept % 2 == 1))
        }
        RoundingMode::HalfUp => round_digit >= 5,
        RoundingMode::HalfDown => round_digit > 5 || (round_digit == 5 && sticky),
    }
}

/// Generate the leading decimal digits of `numerator / denominator`.
///
/// Returns the significant digits (most significant first, without leading
/// zeros), the adjusted exponent of the ratio (the power of ten of its leading
/// digit), whether any non-zero digit was discarded beyond the returned ones,
/// and whether the quotient terminated exactly within the requested digits.
fn significant_digits(
    numerator: u128,
    denominator: u128,
    count: usize,
) -> Result<(Vec<u8>, i32, bool, bool), DecimalError> {
    debug_assert!(numerator != 0 && denominator != 0);
    let integer = numerator / denominator;
    let mut remainder = numerator % denominator;
    let mut digits: Vec<u8> = Vec::with_capacity(count);
    let mut adjusted: i32 = 0;

    if integer != 0 {
        let text = integer.to_string();
        let bytes = text.as_bytes();
        adjusted = (bytes.len() - 1) as i32;
        if bytes.len() >= count {
            for &byte in &bytes[..count] {
                digits.push(byte - b'0');
            }
            let mut more = remainder != 0;
            if bytes[count..].iter().any(|&byte| byte != b'0') {
                more = true;
            }
            return Ok((digits, adjusted, more, false));
        }
        for &byte in bytes {
            digits.push(byte - b'0');
        }
        if remainder == 0 {
            return Ok((digits, adjusted, false, true));
        }
    }

    let mut leading_zeros: i32 = 0;
    while remainder != 0 && digits.len() < count {
        let scaled = remainder.checked_mul(10).ok_or(DecimalError::Overflow)?;
        let digit = (scaled / denominator) as u8;
        remainder = scaled % denominator;
        if digits.is_empty() && digit == 0 {
            leading_zeros += 1;
            continue;
        }
        if digits.is_empty() {
            adjusted = -(leading_zeros + 1);
        }
        digits.push(digit);
    }

    let more = remainder != 0;
    let exact = remainder == 0;
    Ok((digits, adjusted, more, exact))
}

/// Compare the magnitudes of two non-zero decimals without aligning scales.
fn cmp_magnitudes(left: u128, left_scale: i32, right: u128, right_scale: i32) -> Ordering {
    let left_digits = decimal_digits_u128(left);
    let right_digits = decimal_digits_u128(right);
    let left_adjusted = left_digits as i64 - 1 - i64::from(left_scale);
    let right_adjusted = right_digits as i64 - 1 - i64::from(right_scale);
    if left_adjusted != right_adjusted {
        return left_adjusted.cmp(&right_adjusted);
    }
    let span = left_digits.max(right_digits) as usize;
    left.to_string()
        .bytes()
        .chain(core::iter::repeat(b'0'))
        .take(span)
        .cmp(
            right
                .to_string()
                .bytes()
                .chain(core::iter::repeat(b'0'))
                .take(span),
        )
}

/// Round `magnitude / divisor` to an integer under `mode`.
fn round_div_u128(magnitude: u128, divisor: u128, negative: bool, mode: RoundingMode) -> u128 {
    debug_assert!(divisor != 0);
    let quotient = magnitude / divisor;
    let remainder = magnitude % divisor;
    if remainder == 0 {
        return quotient;
    }
    let increment = match mode {
        RoundingMode::Truncate => false,
        RoundingMode::Floor => negative,
        RoundingMode::Ceiling => !negative,
        RoundingMode::HalfEven => match remainder.checked_mul(2) {
            None => true,
            Some(twice) => twice > divisor || (twice == divisor && quotient & 1 == 1),
        },
        RoundingMode::HalfUp => match remainder.checked_mul(2) {
            None => true,
            Some(twice) => twice >= divisor,
        },
        RoundingMode::HalfDown => match remainder.checked_mul(2) {
            None => true,
            Some(twice) => twice > divisor,
        },
    };
    if increment {
        quotient + 1
    } else {
        quotient
    }
}

/// Multiply a coefficient by `10^shift`, erroring when it cannot be exact.
fn shift_coefficient(coefficient: u128, shift: u32) -> Result<U256, DecimalError> {
    if coefficient == 0 {
        return Ok(U256::ZERO);
    }
    if shift == 0 {
        return Ok(U256::from_u128(coefficient));
    }
    let factor = pow10_u128(shift).ok_or(DecimalError::Overflow)?;
    Ok(U256::mul_u128(coefficient, factor))
}

/// `10^exponent` as a `u128`, or `None` when it does not fit.
fn pow10_u128(exponent: u32) -> Option<u128> {
    let mut result: u128 = 1;
    let mut step = 0;
    while step < exponent {
        result = result.checked_mul(10)?;
        step += 1;
    }
    Some(result)
}

/// The number of decimal digits in `value` (zero has one digit).
fn decimal_digits_u128(value: u128) -> u32 {
    if value == 0 {
        1
    } else {
        value.ilog10() + 1
    }
}

/// Assemble a `u128` from big-endian decimal digits.
fn digits_to_u128(digits: &[u8]) -> Option<u128> {
    let mut value: u128 = 0;
    for &digit in digits {
        value = value.checked_mul(10)?.checked_add(u128::from(digit))?;
    }
    Some(value)
}

/// Convert a 256-bit magnitude into big-endian decimal digits.
fn u256_to_digits(mut magnitude: U256) -> Vec<u8> {
    if magnitude.is_zero() {
        return vec![0];
    }
    let mut reversed = Vec::new();
    while !magnitude.is_zero() {
        let (quotient, remainder) = magnitude.div_rem_u32(10);
        reversed.push(remainder as u8);
        magnitude = quotient;
    }
    reversed.reverse();
    reversed
}

/// Convert a sign flag and magnitude into a signed `i128`.
fn signed_i128(negative: bool, magnitude: u128) -> Result<i128, DecimalError> {
    let limit = if negative {
        (i128::MAX as u128) + 1
    } else {
        i128::MAX as u128
    };
    if magnitude > limit {
        return Err(DecimalError::Overflow);
    }
    let value = magnitude as i128;
    Ok(if negative {
        value.wrapping_neg()
    } else {
        value
    })
}

impl FromStr for Decimal {
    type Err = DecimalError;

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
        let tail = &text[index..];
        if tail.eq_ignore_ascii_case("nan")
            || tail.eq_ignore_ascii_case("snan")
            || tail.eq_ignore_ascii_case("inf")
            || tail.eq_ignore_ascii_case("infinity")
        {
            return Err(DecimalError::NonFinite);
        }

        let mut fraction_digits: i64 = 0;
        let mut seen_digit = false;
        let mut seen_point = false;
        let mut digits: Vec<u8> = Vec::new();

        while index < bytes.len() {
            match bytes[index] {
                digit @ b'0'..=b'9' => {
                    seen_digit = true;
                    digits.push(digit - b'0');
                    if seen_point {
                        fraction_digits += 1;
                    }
                    index += 1;
                }
                b'_' if seen_digit
                    && index + 1 < bytes.len()
                    && bytes[index + 1].is_ascii_digit() =>
                {
                    index += 1;
                }
                b'.' if !seen_point => {
                    seen_point = true;
                    index += 1;
                }
                b'e' | b'E' => break,
                _ => return Err(DecimalError::InvalidText),
            }
        }
        if !seen_digit {
            return Err(DecimalError::InvalidText);
        }
        let coefficient = digits_to_u128(&digits).ok_or(DecimalError::Overflow)?;

        let mut exponent: i64 = 0;
        if index < bytes.len() {
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
            while index < bytes.len() {
                match bytes[index] {
                    digit @ b'0'..=b'9' => {
                        exponent = exponent
                            .checked_mul(10)
                            .and_then(|value| value.checked_add(i64::from(digit - b'0')))
                            .ok_or(DecimalError::Overflow)?;
                        index += 1;
                    }
                    b'_' if index + 1 < bytes.len() && bytes[index + 1].is_ascii_digit() => {
                        index += 1;
                    }
                    _ => return Err(DecimalError::InvalidText),
                }
            }
            if index == start {
                return Err(DecimalError::InvalidText);
            }
            if exponent_negative {
                exponent = -exponent;
            }
        }

        let scale = fraction_digits
            .checked_sub(exponent)
            .ok_or(DecimalError::Overflow)?;
        let scale = i32::try_from(scale).map_err(|_| DecimalError::Overflow)?;
        Ok(Self {
            negative,
            coefficient,
            scale,
        })
    }
}

impl fmt::Display for Decimal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.negative {
            formatter.write_str("-")?;
        }
        let mut buffer = [0u8; 39];
        let count = write_digits(self.coefficient, &mut buffer);
        let digits = &buffer[..count];
        let exponent = -i64::from(self.scale);
        let adjusted = exponent + count as i64 - 1;
        if exponent <= 0 && adjusted >= -6 {
            if exponent == 0 {
                write_ascii(formatter, digits)?;
            } else {
                let point = count as i64 + exponent;
                if point > 0 {
                    let point = point as usize;
                    write_ascii(formatter, &digits[..point])?;
                    formatter.write_str(".")?;
                    write_ascii(formatter, &digits[point..])?;
                } else {
                    formatter.write_str("0.")?;
                    for _ in 0..(-point as usize) {
                        formatter.write_str("0")?;
                    }
                    write_ascii(formatter, digits)?;
                }
            }
        } else {
            write_ascii(formatter, &digits[..1])?;
            if count > 1 {
                formatter.write_str(".")?;
                write_ascii(formatter, &digits[1..])?;
            }
            formatter.write_str("E")?;
            if adjusted >= 0 {
                formatter.write_str("+")?;
            }
            write!(formatter, "{adjusted}")?;
        }
        Ok(())
    }
}

/// Write the decimal digits of `value` big-endian into `buffer`.
fn write_digits(mut value: u128, buffer: &mut [u8; 39]) -> usize {
    if value == 0 {
        buffer[0] = b'0';
        return 1;
    }
    let mut index = buffer.len();
    while value > 0 {
        index -= 1;
        buffer[index] = b'0' + (value % 10) as u8;
        value /= 10;
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

impl PartialEq for Decimal {
    fn eq(&self, other: &Self) -> bool {
        self.cmp_value(*other) == Ordering::Equal
    }
}

impl Eq for Decimal {}

impl PartialOrd for Decimal {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(Ord::cmp(self, other))
    }
}

impl Ord for Decimal {
    fn cmp(&self, other: &Self) -> Ordering {
        self.cmp_value(*other)
    }
}

impl Hash for Decimal {
    fn hash<H: Hasher>(&self, state: &mut H) {
        if self.coefficient == 0 {
            state.write_u8(0);
            return;
        }
        let mut coefficient = self.coefficient;
        let mut scale = self.scale;
        while coefficient.is_multiple_of(10) {
            coefficient /= 10;
            scale -= 1;
        }
        state.write_u8(u8::from(self.negative));
        state.write_u128(coefficient);
        state.write_i32(scale);
    }
}

impl Add for Decimal {
    type Output = Self;
    fn add(self, rhs: Self) -> Self {
        self.checked_add_with(rhs, &Context::default())
            .expect("Decimal addition overflow")
    }
}

impl Sub for Decimal {
    type Output = Self;
    fn sub(self, rhs: Self) -> Self {
        self.checked_sub_with(rhs, &Context::default())
            .expect("Decimal subtraction overflow")
    }
}

impl Mul for Decimal {
    type Output = Self;
    fn mul(self, rhs: Self) -> Self {
        self.checked_mul_with(rhs, &Context::default())
            .expect("Decimal multiplication overflow")
    }
}

impl Div for Decimal {
    type Output = Self;
    fn div(self, rhs: Self) -> Self {
        self.checked_div_with(rhs, &Context::default())
            .expect("Decimal division failed")
    }
}

impl Neg for Decimal {
    type Output = Self;
    fn neg(self) -> Self {
        self.negated()
    }
}

impl AddAssign for Decimal {
    fn add_assign(&mut self, rhs: Self) {
        *self = *self + rhs;
    }
}

impl SubAssign for Decimal {
    fn sub_assign(&mut self, rhs: Self) {
        *self = *self - rhs;
    }
}

impl MulAssign for Decimal {
    fn mul_assign(&mut self, rhs: Self) {
        *self = *self * rhs;
    }
}

impl DivAssign for Decimal {
    fn div_assign(&mut self, rhs: Self) {
        *self = *self / rhs;
    }
}

impl From<i128> for Decimal {
    fn from(value: i128) -> Self {
        Self::from_i128(value)
    }
}

impl From<u128> for Decimal {
    fn from(value: u128) -> Self {
        Self::from_u128(value)
    }
}

impl From<i64> for Decimal {
    fn from(value: i64) -> Self {
        Self::from_i128(i128::from(value))
    }
}

impl From<u64> for Decimal {
    fn from(value: u64) -> Self {
        Self::from_u128(u128::from(value))
    }
}

impl From<i32> for Decimal {
    fn from(value: i32) -> Self {
        Self::from_i128(i128::from(value))
    }
}

impl From<u32> for Decimal {
    fn from(value: u32) -> Self {
        Self::from_u128(u128::from(value))
    }
}

impl TryFrom<Decimal> for f64 {
    type Error = DecimalError;

    fn try_from(value: Decimal) -> Result<Self, Self::Error> {
        value.to_f64_checked()
    }
}
