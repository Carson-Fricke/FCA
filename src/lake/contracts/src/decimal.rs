//! Scale-18 fixed-point decimal value.
//!
//! The lake persists `decimal128(38, 18)` prices and quantities. This value
//! holds the same unscaled `i128`, parses exact decimal literals without
//! rounding, and renders the canonical shortest representation Python's
//! `str(Decimal)` produces for typical provider payloads.

use core::fmt;
use core::str::FromStr;

use serde::de::{self, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::error::DataError;

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
    /// The exact value is outside `decimal128(38, 18)`.
    Overflow,
    /// The input text is not a finite decimal literal with at most 18
    /// fractional digits after exponent scaling.
    InvalidText,
}

impl fmt::Display for DecimalError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Overflow => {
                formatter.write_str("fixed-point value is outside decimal128(38, 18)")
            }
            Self::InvalidText => formatter.write_str("invalid decimal literal"),
        }
    }
}

impl std::error::Error for DecimalError {}

impl From<DecimalError> for DataError {
    fn from(error: DecimalError) -> Self {
        Self::Arithmetic(error.to_string())
    }
}

/// An `i128` unscaled value at scale 18.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Decimal18(i128);

impl Decimal18 {
    /// `0`.
    pub const ZERO: Self = Self(0);
    /// `1`.
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

    /// Build from an unscaled integer without a range check.
    ///
    /// Callers that already validated the value (for example a `cast` of a
    /// `decimal128(38, 18)` column) use this to avoid a redundant check.
    pub const fn from_unscaled_unchecked(unscaled: i128) -> Self {
        Self(unscaled)
    }

    /// The unscaled integer.
    pub const fn unscaled(self) -> i128 {
        self.0
    }

    /// Parse an exact decimal literal.
    pub fn parse(text: &str) -> Result<Self, DecimalError> {
        let trimmed = text.trim();
        if trimmed.is_empty() {
            return Err(DecimalError::InvalidText);
        }
        let mut chars = trimmed.chars().peekable();
        let negative = match chars.peek() {
            Some('+') => {
                chars.next();
                false
            }
            Some('-') => {
                chars.next();
                true
            }
            _ => false,
        };
        let mut digits: i128 = 0;
        let mut saw_digit = false;
        let mut fraction_digits: i64 = 0;
        let mut in_fraction = false;
        while let Some(&ch) = chars.peek() {
            match ch {
                '0'..='9' => {
                    chars.next();
                    saw_digit = true;
                    let digit = (ch as u8 - b'0') as i128;
                    digits = digits
                        .checked_mul(10)
                        .and_then(|value| value.checked_add(digit))
                        .ok_or(DecimalError::Overflow)?;
                    if in_fraction {
                        fraction_digits += 1;
                    }
                }
                '.' if !in_fraction => {
                    chars.next();
                    in_fraction = true;
                }
                _ => break,
            }
        }
        if !saw_digit {
            return Err(DecimalError::InvalidText);
        }
        let mut exponent: i64 = 0;
        if let Some(&ch) = chars.peek() {
            if ch == 'e' || ch == 'E' {
                chars.next();
                let mut sign = 1i64;
                if let Some(&next) = chars.peek() {
                    if next == '+' {
                        chars.next();
                    } else if next == '-' {
                        sign = -1;
                        chars.next();
                    }
                }
                let mut value: i64 = 0;
                let mut saw_exponent = false;
                for next in chars.by_ref() {
                    if !next.is_ascii_digit() {
                        return Err(DecimalError::InvalidText);
                    }
                    saw_exponent = true;
                    value = value
                        .checked_mul(10)
                        .and_then(|number| number.checked_add((next as u8 - b'0') as i64))
                        .ok_or(DecimalError::Overflow)?;
                }
                if !saw_exponent {
                    return Err(DecimalError::InvalidText);
                }
                exponent = sign * value;
            }
        }
        if chars.next().is_some() {
            return Err(DecimalError::InvalidText);
        }
        let shift = exponent - fraction_digits + SCALE as i64;
        let unscaled = if shift >= 0 {
            let factor = pow10(shift as u32).ok_or(DecimalError::Overflow)?;
            digits.checked_mul(factor).ok_or(DecimalError::Overflow)?
        } else {
            let factor = pow10((-shift) as u32).ok_or(DecimalError::Overflow)?;
            if digits % factor != 0 {
                return Err(DecimalError::InvalidText);
            }
            digits / factor
        };
        let signed = if negative { -unscaled } else { unscaled };
        Self::from_unscaled(signed)
    }

    /// Whether the value is strictly positive.
    pub const fn is_positive(self) -> bool {
        self.0 > 0
    }

    /// Whether the value is strictly negative.
    pub const fn is_negative(self) -> bool {
        self.0 < 0
    }

    /// Whether the value is zero.
    pub const fn is_zero(self) -> bool {
        self.0 == 0
    }

    /// The canonical shortest decimal rendering with trailing zeros trimmed.
    pub fn to_canonical_string(self) -> String {
        if self.0 == 0 {
            return "0".to_string();
        }
        let negative = self.0 < 0;
        let magnitude = self.0.unsigned_abs();
        let integer = magnitude / ONE_UNSCALED as u128;
        let fraction = magnitude % ONE_UNSCALED as u128;
        let mut text = if fraction == 0 {
            format!("{integer}")
        } else {
            let digits = format!("{fraction:018}");
            let trimmed = digits.trim_end_matches('0');
            format!("{integer}.{trimmed}")
        };
        if negative {
            text.insert(0, '-');
        }
        text
    }
}

fn pow10(exponent: u32) -> Option<i128> {
    let mut value: i128 = 1;
    for _ in 0..exponent {
        value = value.checked_mul(10)?;
    }
    Some(value)
}

impl FromStr for Decimal18 {
    type Err = DecimalError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        Self::parse(text)
    }
}

impl fmt::Display for Decimal18 {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.to_canonical_string())
    }
}

impl Serialize for Decimal18 {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&self.to_canonical_string())
    }
}

struct DecimalVisitor;

impl Visitor<'_> for DecimalVisitor {
    type Value = Decimal18;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a decimal string or integer")
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Decimal18::parse(value).map_err(|error| E::custom(error.to_string()))
    }

    fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Decimal18::parse(&value.to_string()).map_err(|error| E::custom(error.to_string()))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Decimal18::parse(&value.to_string()).map_err(|error| E::custom(error.to_string()))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Decimal18::parse(&format!("{value}")).map_err(|error| E::custom(error.to_string()))
    }
}

impl<'de> Deserialize<'de> for Decimal18 {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        deserializer.deserialize_any(DecimalVisitor)
    }
}

#[cfg(test)]
#[path = "../tests/unit/decimal.rs"]
mod tests;
