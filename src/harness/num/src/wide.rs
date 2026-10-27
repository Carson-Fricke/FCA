//! Minimal 256-bit unsigned arithmetic backing the scale-18 decimal kernel.
//!
//! The fixed-point operations need a 256-bit intermediate: multiplying two
//! `i128` unscaled values produces up to 256 bits, and dividing a scale-18
//! value widens the numerator by `10^18` before the division. This module
//! provides only the operations the kernel uses, all on the stack — no
//! allocation.

/// A 256-bit unsigned integer built from two 128-bit limbs.
///
/// The high limb is declared first so the derived `Ord` compares most
/// significant bits first.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct U256 {
    pub(crate) high: u128,
    pub(crate) low: u128,
}

impl U256 {
    /// The additive identity.
    pub(crate) const ZERO: Self = Self { high: 0, low: 0 };

    /// Widen a 128-bit value.
    pub(crate) const fn from_u128(value: u128) -> Self {
        Self {
            high: 0,
            low: value,
        }
    }

    /// True when the value is zero.
    pub(crate) const fn is_zero(self) -> bool {
        self.high == 0 && self.low == 0
    }

    /// The bit at `index`, counting from the least significant bit.
    pub(crate) const fn bit(self, index: u32) -> bool {
        if index < 128 {
            (self.low >> index) & 1 == 1
        } else {
            (self.high >> (index - 128)) & 1 == 1
        }
    }

    /// Return the value with the bit at `index` set.
    pub(crate) const fn with_bit(self, index: u32) -> Self {
        if index < 128 {
            Self {
                high: self.high,
                low: self.low | (1u128 << index),
            }
        } else {
            Self {
                high: self.high | (1u128 << (index - 128)),
                low: self.low,
            }
        }
    }

    /// Shift left by one bit, preserving all 256 bits.
    pub(crate) const fn shl1(self) -> Self {
        let low = self.low << 1;
        let carry = self.low >> 127;
        let high = (self.high << 1) | carry;
        Self { high, low }
    }

    /// Checked addition. Returns `None` on overflow.
    pub(crate) const fn checked_add(self, other: Self) -> Option<Self> {
        let (low, carry) = self.low.overflowing_add(other.low);
        let (high, overflow) = self.high.overflowing_add(other.high);
        if overflow {
            return None;
        }
        let (high, overflow) = high.overflowing_add(carry as u128);
        if overflow {
            return None;
        }
        Some(Self { high, low })
    }

    /// Wrapping subtraction. Callers guarantee `self >= other`.
    pub(crate) const fn wrapping_sub(self, other: Self) -> Self {
        let (low, borrow) = self.low.overflowing_sub(other.low);
        let high = self
            .high
            .wrapping_sub(other.high)
            .wrapping_sub(borrow as u128);
        Self { high, low }
    }

    /// Multiply two 128-bit values into a 256-bit product.
    ///
    /// The operands are split into 32-bit limbs so every partial product fits
    /// comfortably in a `u128` accumulator.
    pub(crate) fn mul_u128(lhs: u128, rhs: u128) -> Self {
        let lhs = limbs32(lhs);
        let rhs = limbs32(rhs);
        let mut accumulators = [0u128; 8];
        for (lhs_index, &lhs_limb) in lhs.iter().enumerate() {
            let lhs_wide = u128::from(lhs_limb);
            for (rhs_index, &rhs_limb) in rhs.iter().enumerate() {
                accumulators[lhs_index + rhs_index] += lhs_wide * u128::from(rhs_limb);
            }
        }
        let mut limbs = [0u32; 8];
        let mut carry: u128 = 0;
        for (index, slot) in limbs.iter_mut().enumerate() {
            let value = accumulators[index] + carry;
            *slot = (value & 0xFFFF_FFFF) as u32;
            carry = value >> 32;
        }
        debug_assert_eq!(carry, 0, "256-bit product cannot carry past eight limbs");
        Self {
            low: pack32(&limbs[0..4]),
            high: pack32(&limbs[4..8]),
        }
    }

    /// Divide by a small 32-bit value, returning the quotient and the `u32`
    /// remainder. Used by decimal digit extraction. `divisor` must be non-zero.
    pub(crate) fn div_rem_u32(self, divisor: u32) -> (Self, u32) {
        debug_assert!(divisor != 0, "division by zero");
        let limbs = [
            self.low as u64,
            (self.low >> 64) as u64,
            self.high as u64,
            (self.high >> 64) as u64,
        ];
        let divisor = u128::from(divisor);
        let mut remainder: u128 = 0;
        let mut quotient_limbs = [0u64; 4];
        for index in (0..4).rev() {
            let current = (remainder << 64) | u128::from(limbs[index]);
            quotient_limbs[index] = (current / divisor) as u64;
            remainder = current % divisor;
        }
        let low = u128::from(quotient_limbs[0]) | (u128::from(quotient_limbs[1]) << 64);
        let high = u128::from(quotient_limbs[2]) | (u128::from(quotient_limbs[3]) << 64);
        (Self { high, low }, remainder as u32)
    }

    /// Divide `numerator` by `denominator`, returning the quotient and remainder.
    ///
    /// Narrow hardware division or aligned long division; `denominator` must
    /// be non-zero. The wide loop visits only possible quotient bits.
    pub(crate) fn div_rem(numerator: Self, denominator: Self) -> (Self, Self) {
        debug_assert!(!denominator.is_zero(), "division by zero");
        if numerator < denominator {
            return (Self::ZERO, numerator);
        }
        if numerator.high == 0 {
            return (
                Self::from_u128(numerator.low / denominator.low),
                Self::from_u128(numerator.low % denominator.low),
            );
        }
        let shift = denominator.leading_zeros() - numerator.leading_zeros();
        let mut divisor = denominator.shift_left(shift);
        let mut quotient = Self::ZERO;
        let mut remainder = numerator;
        for index in (0..=shift).rev() {
            if remainder >= divisor {
                remainder = remainder.wrapping_sub(divisor);
                quotient = quotient.with_bit(index);
            }
            divisor = Self {
                high: divisor.high >> 1,
                low: (divisor.low >> 1) | (divisor.high << 127),
            };
        }
        (quotient, remainder)
    }

    fn leading_zeros(self) -> u32 {
        if self.high == 0 {
            128 + self.low.leading_zeros()
        } else {
            self.high.leading_zeros()
        }
    }

    /// The caller aligns a nonzero divisor without discarding significant bits.
    fn shift_left(self, shift: u32) -> Self {
        match shift {
            0 => self,
            1..=127 => Self {
                high: (self.high << shift) | (self.low >> (128 - shift)),
                low: self.low << shift,
            },
            _ => Self {
                high: self.low << (shift - 128),
                low: 0,
            },
        }
    }
}

/// Split a 128-bit value into four little-endian 32-bit limbs.
fn limbs32(value: u128) -> [u32; 4] {
    [
        value as u32,
        (value >> 32) as u32,
        (value >> 64) as u32,
        (value >> 96) as u32,
    ]
}

/// Pack four little-endian 32-bit limbs into a 128-bit value.
fn pack32(limbs: &[u32]) -> u128 {
    let mut value = 0u128;
    for (index, &limb) in limbs.iter().enumerate() {
        value |= u128::from(limb) << (index * 32);
    }
    value
}
