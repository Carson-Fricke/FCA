//! Fixed-point rounding is unchanged by operand cancellation or narrow kernels.
//! The independent bit-at-a-time oracle is deliberately slow and test-only.
use num::{Decimal18, DecimalError, RoundingMode, MAX_UNSCALED, ONE_UNSCALED};

fn reference(lhs: i128, rhs: i128, divisor: i128, mode: RoundingMode) -> Result<Decimal18, DecimalError> {
    if divisor == 0 {
        return Err(DecimalError::DivisionByZero);
    }
    let negative = (lhs < 0) ^ (rhs < 0) ^ (divisor < 0);
    let a = lhs.unsigned_abs();
    let b = rhs.unsigned_abs();
    let d = divisor.unsigned_abs();
    let mut product = [0u32; 8];
    let mut carry = 0u128;
    for index in 0..8 {
        let mut sum = carry;
        for left in 0..4 {
            if index >= left && index - left < 4 {
                sum += ((a >> (left * 32)) & 0xffff_ffff)
                    * ((b >> ((index - left) * 32)) & 0xffff_ffff);
            }
        }
        product[index] = sum as u32;
        carry = sum >> 32;
    }
    let mut quotient = [0u32; 8];
    let mut remainder = 0u128;
    for index in (0..256).rev() {
        // Decimal divisors are below 2^127, so the shifted remainder fits.
        remainder = (remainder << 1) | u128::from((product[index / 32] >> (index % 32)) & 1);
        if remainder >= d {
            remainder -= d;
            quotient[index / 32] |= 1 << (index % 32);
        }
    }
    let mut magnitude = u128::from(quotient[0]) | (u128::from(quotient[1]) << 32)
        | (u128::from(quotient[2]) << 64) | (u128::from(quotient[3]) << 96);
    if quotient[4..].iter().any(|&limb| limb != 0) {
        return Err(DecimalError::Overflow);
    }
    let increment = remainder != 0 && match mode {
        RoundingMode::Truncate => false,
        RoundingMode::Floor => negative,
        RoundingMode::Ceiling => !negative,
        RoundingMode::HalfEven => remainder * 2 > d || (remainder * 2 == d && magnitude & 1 == 1),
        RoundingMode::HalfUp => remainder * 2 >= d,
        RoundingMode::HalfDown => remainder * 2 > d,
    };
    if increment {
        magnitude = magnitude.checked_add(1).ok_or(DecimalError::Overflow)?;
    }
    if magnitude > MAX_UNSCALED as u128 {
        return Err(DecimalError::Overflow);
    }
    Decimal18::from_unscaled(if negative { -(magnitude as i128) } else { magnitude as i128 })
}

#[test]
fn exact_ratios_match_independent_wide_rounding_at_all_operand_sizes() {
    let modes = [RoundingMode::HalfEven, RoundingMode::HalfUp, RoundingMode::HalfDown,
        RoundingMode::Truncate, RoundingMode::Floor, RoundingMode::Ceiling];
    for mode in modes {
        for sign in [-1, 1] {
            for a in [ONE_UNSCALED + 1, ONE_UNSCALED + 3, MAX_UNSCALED] {
                let b = 10i128.pow(35);
                let d = b * 2;
                assert_eq!(Decimal18::from_unscaled(sign * a).unwrap().checked_mul_div_with(
                    Decimal18::from_unscaled(b).unwrap(), Decimal18::from_unscaled(d).unwrap(), mode),
                    reference(sign * a, b, d, mode));
            }
        }
    }
    let mut seed = 0x91e1_0da5_c79e_7b1du64;
    let mut next = || {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        seed
    };
    for index in 0..192 {
        let values: Vec<_> = (0..3).map(|offset| {
            let raw = (u128::from(next()) << 64) | u128::from(next());
            let bound = 10u128.pow(((index + offset * 13) % 38 + 1) as u32);
            let value = (raw % bound) as i128;
            if next() & 1 == 0 { value } else { -value }
        }).collect();
        let [a, b, d] = [values[0], values[1], values[2]];
        let lhs = Decimal18::from_unscaled(a).unwrap();
        let rhs = Decimal18::from_unscaled(b).unwrap();
        let divisor = Decimal18::from_unscaled(d).unwrap();
        assert_eq!(lhs.checked_mul(rhs), reference(a, b, ONE_UNSCALED, RoundingMode::HalfEven));
        for mode in modes {
            assert_eq!(lhs.checked_div_with(divisor, mode), reference(a, ONE_UNSCALED, d, mode), "divide {a}/{d} {mode:?}");
            assert_eq!(lhs.checked_mul_div_with(rhs, divisor, mode), reference(a, b, d, mode), "ratio {a}*{b}/{d} {mode:?}");
        }
    }
    for a in [0, 1, -1, ONE_UNSCALED, -ONE_UNSCALED, MAX_UNSCALED, -MAX_UNSCALED] {
        for b in [0, 2, -2, ONE_UNSCALED, MAX_UNSCALED] {
            for d in [0, 2, -2, ONE_UNSCALED, MAX_UNSCALED] {
                for mode in modes {
                    assert_eq!(Decimal18::from_unscaled(a).unwrap().checked_mul_div_with(
                        Decimal18::from_unscaled(b).unwrap(), Decimal18::from_unscaled(d).unwrap(), mode),
                        reference(a, b, d, mode), "edge {a}*{b}/{d} {mode:?}");
                }
            }
        }
    }
}
