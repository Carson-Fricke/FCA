//! Golden-vector and property tests for the scale-18 fixed-point kernel.
//!
//! The golden vectors in `golden_data` are generated from the Python
//! core-types reference by `generate_golden.py`. The property tests are
//! hand-rolled and deterministic (a fixed-seed xorshift), so no extra
//! dev-dependency is required.

use core::str::FromStr;

use num::{Decimal18, DecimalError, RoundingMode, MAX_UNSCALED, MIN_UNSCALED, ONE_UNSCALED};

mod golden_data;

use golden_data::{
    BinaryCase, IntCase, MulDivCase, ParseCase, RoundCase, TextCase, BINARY_CASES, INT_CASES,
    MUL_DIV_CASES, PARSE_CASES, ROUND_CASES, TEXT_CASES,
};

fn mode(name: &str) -> RoundingMode {
    RoundingMode::from_name(name).unwrap_or_else(|| panic!("unknown rounding mode {name}"))
}

fn assert_unscaled(result: Result<Decimal18, DecimalError>, expected: Option<i128>, label: &str) {
    match expected {
        Some(value) => assert_eq!(
            result
                .unwrap_or_else(|error| panic!("{label}: {error}"))
                .unscaled(),
            value,
            "{label}"
        ),
        None => assert!(
            result.is_err(),
            "{label}: expected an error, got {result:?}"
        ),
    }
}

#[test]
fn binary_cases_match_python_reference() {
    for case in BINARY_CASES {
        let BinaryCase {
            a,
            b,
            add,
            sub,
            mul,
            div,
        } = *case;
        let lhs = Decimal18::from_unscaled(a).unwrap();
        let rhs = Decimal18::from_unscaled(b).unwrap();
        assert_unscaled(lhs.checked_add(rhs), add, &format!("add({a}, {b})"));
        assert_unscaled(lhs.checked_sub(rhs), sub, &format!("sub({a}, {b})"));
        assert_unscaled(lhs.checked_mul(rhs), mul, &format!("mul({a}, {b})"));
        if b != 0 {
            assert_unscaled(lhs.checked_div(rhs), div, &format!("div({a}, {b})"));
        }
    }
}

#[test]
fn round_cases_match_python_reference() {
    for case in ROUND_CASES {
        let RoundCase {
            op,
            a,
            b,
            mode: mode_name,
            expected,
        } = *case;
        let value = Decimal18::from_unscaled(a).unwrap();
        let result = match op {
            "div" => value.checked_div_with(Decimal18::from_unscaled(b).unwrap(), mode(mode_name)),
            "round_to" => value.round_to(mode(mode_name), u32::try_from(b).unwrap()),
            other => panic!("unknown round op {other}"),
        };
        assert_unscaled(result, expected, &format!("{op}({a}, {b}, {mode_name})"));
    }
}

#[test]
fn mul_div_cases_match_python_reference() {
    for case in MUL_DIV_CASES {
        let MulDivCase {
            a,
            b,
            c,
            mode: mode_name,
            expected,
        } = *case;
        let result = Decimal18::from_unscaled(a).unwrap().checked_mul_div_with(
            Decimal18::from_unscaled(b).unwrap(),
            Decimal18::from_unscaled(c).unwrap(),
            mode(mode_name),
        );
        assert_unscaled(
            result,
            expected,
            &format!("mul_div({a}, {b}, {c}, {mode_name})"),
        );
    }
}

#[test]
fn text_cases_match_python_reference() {
    for case in TEXT_CASES {
        let TextCase { unscaled, text } = *case;
        assert_eq!(
            Decimal18::from_unscaled(unscaled).unwrap().to_string(),
            text,
            "display({unscaled})"
        );
    }
}

#[test]
fn parse_cases_match_python_reference() {
    for case in PARSE_CASES {
        let ParseCase { text, unscaled } = *case;
        let parsed = Decimal18::from_str(text);
        match unscaled {
            Some(value) => assert_eq!(
                parsed
                    .unwrap_or_else(|error| panic!("parse({text:?}) failed: {error}"))
                    .unscaled(),
                value,
                "parse({text:?})"
            ),
            None => assert!(parsed.is_err(), "parse({text:?}) should fail"),
        }
    }
}

#[test]
fn int_cases_match_python_reference() {
    for case in INT_CASES {
        let IntCase {
            unscaled,
            mode: mode_name,
            integer,
        } = *case;
        assert_eq!(
            Decimal18::from_unscaled(unscaled)
                .unwrap()
                .to_i128(mode(mode_name))
                .unwrap(),
            integer,
            "to_i128({unscaled}, {mode_name})"
        );
    }
}

#[test]
fn division_by_zero_is_reported() {
    let one = Decimal18::ONE;
    assert_eq!(
        one.checked_div(Decimal18::ZERO),
        Err(DecimalError::DivisionByZero)
    );
    assert_eq!(
        one.checked_div_with(Decimal18::ZERO, RoundingMode::Floor),
        Err(DecimalError::DivisionByZero)
    );
    assert_eq!(
        one.checked_mul_div(one, Decimal18::ZERO),
        Err(DecimalError::DivisionByZero)
    );
}

#[test]
fn overflow_edges_are_reported() {
    let max = Decimal18::MAX;
    let min = Decimal18::MIN;
    assert_eq!(max.checked_add(Decimal18::ONE), Err(DecimalError::Overflow));
    assert_eq!(min.checked_sub(Decimal18::ONE), Err(DecimalError::Overflow));
    assert_eq!(
        max.checked_mul(Decimal18::from(2)),
        Err(DecimalError::Overflow)
    );
    assert_eq!(
        min.checked_mul(Decimal18::from(2)),
        Err(DecimalError::Overflow)
    );
    assert_eq!(
        Decimal18::from_unscaled(MAX_UNSCALED + 1),
        Err(DecimalError::Overflow)
    );
    assert_eq!(
        Decimal18::from_unscaled(MIN_UNSCALED - 1),
        Err(DecimalError::Overflow)
    );
    assert_eq!(Decimal18::from_i128(i128::MAX), Err(DecimalError::Overflow));
    assert_eq!(Decimal18::from_f64(f64::NAN), Err(DecimalError::NonFinite));
    assert_eq!(
        Decimal18::from_f64(f64::INFINITY),
        Err(DecimalError::NonFinite)
    );
    assert_eq!(
        Decimal18::ONE.round_to(RoundingMode::HalfEven, 19),
        Err(DecimalError::InvalidScale)
    );
}

#[test]
fn rounding_edges_are_explicit() {
    let half = Decimal18::from_unscaled(500_000_000_000_000_000).unwrap();
    let one_and_half = Decimal18::from_unscaled(1_500_000_000_000_000_000).unwrap();
    let two_and_half = Decimal18::from_unscaled(2_500_000_000_000_000_000).unwrap();
    let negative_half = Decimal18::from_unscaled(-500_000_000_000_000_000).unwrap();

    assert_eq!(half.to_i128(RoundingMode::HalfEven).unwrap(), 0);
    assert_eq!(half.to_i128(RoundingMode::HalfUp).unwrap(), 1);
    assert_eq!(half.to_i128(RoundingMode::HalfDown).unwrap(), 0);
    assert_eq!(half.to_i128(RoundingMode::Truncate).unwrap(), 0);
    assert_eq!(half.to_i128(RoundingMode::Floor).unwrap(), 0);
    assert_eq!(half.to_i128(RoundingMode::Ceiling).unwrap(), 1);

    assert_eq!(one_and_half.to_i128(RoundingMode::HalfEven).unwrap(), 2);
    assert_eq!(two_and_half.to_i128(RoundingMode::HalfEven).unwrap(), 2);
    assert_eq!(two_and_half.to_i128(RoundingMode::HalfUp).unwrap(), 3);

    assert_eq!(negative_half.to_i128(RoundingMode::HalfEven).unwrap(), 0);
    assert_eq!(negative_half.to_i128(RoundingMode::HalfUp).unwrap(), -1);
    assert_eq!(negative_half.to_i128(RoundingMode::Floor).unwrap(), -1);
    assert_eq!(negative_half.to_i128(RoundingMode::Ceiling).unwrap(), 0);

    // Rounding to fewer places keeps the scale-18 representation.
    let value = Decimal18::from_str("1.234567890123456789").unwrap();
    assert_eq!(
        value
            .round_to(RoundingMode::HalfEven, 6)
            .unwrap()
            .to_string(),
        "1.234568000000000000"
    );
    assert_eq!(
        value
            .round_to(RoundingMode::Truncate, 6)
            .unwrap()
            .to_string(),
        "1.234567000000000000"
    );
    assert_eq!(value.round_to(RoundingMode::HalfEven, 18).unwrap(), value);
}

#[test]
fn float_conversion_round_trips_within_precision() {
    for value in [0.0_f64, 1.0, -1.0, 0.5, 123.456, -0.000_001] {
        let decimal = Decimal18::from_f64(value).unwrap();
        assert!(
            (decimal.to_f64() - value).abs() < 1e-9,
            "round trip {value}"
        );
    }
}

/// Deterministic xorshift generator for the property tests.
struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut state = self.0;
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        self.0 = state;
        state
    }

    fn next_u128(&mut self) -> u128 {
        (u128::from(self.next_u64()) << 64) | u128::from(self.next_u64())
    }

    /// A value in `[-bound, bound]`.
    fn range(&mut self, bound: i128) -> i128 {
        let span = (bound as u128) * 2 + 1;
        (self.next_u128() % span) as i128 - bound
    }
}

fn random_decimal(rng: &mut Rng) -> Decimal18 {
    Decimal18::from_unscaled(rng.range(MAX_UNSCALED / 1000)).unwrap()
}

#[test]
fn property_algebraic_invariants() {
    let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
    for _ in 0..2_000 {
        let a = random_decimal(&mut rng);
        let b = random_decimal(&mut rng);

        if let (Ok(left), Ok(right)) = (a.checked_add(b), b.checked_add(a)) {
            assert_eq!(left, right, "addition is commutative");
        }
        if let (Ok(left), Ok(right)) = (a.checked_mul(b), b.checked_mul(a)) {
            assert_eq!(left, right, "multiplication is commutative");
        }
        assert_eq!(a.checked_add(Decimal18::ZERO).unwrap(), a);
        assert_eq!(a.checked_mul(Decimal18::ONE).unwrap(), a);
        assert_eq!(a.checked_div(Decimal18::ONE).unwrap(), a);
        assert_eq!(
            a.checked_mul_div(Decimal18::ONE, Decimal18::ONE).unwrap(),
            a
        );

        if let Ok(sum) = a.checked_add(b) {
            if let Ok(back) = sum.checked_sub(b) {
                assert_eq!(back, a, "addition and subtraction are inverses");
            }
        }

        let negated = a.checked_neg().unwrap();
        assert_eq!(negated.checked_neg().unwrap(), a);
        assert_eq!(a.checked_add(negated).unwrap(), Decimal18::ZERO);
        assert_eq!(a.abs().unscaled(), a.unscaled().abs());

        if !b.is_zero() {
            if let (Ok(quotient), Ok(negated_quotient)) = (a.checked_div(b), negated.checked_div(b))
            {
                assert_eq!(
                    negated_quotient,
                    quotient.checked_neg().unwrap(),
                    "half-even division is sign-symmetric"
                );
            }
        }

        for mode in [
            RoundingMode::HalfEven,
            RoundingMode::HalfUp,
            RoundingMode::HalfDown,
            RoundingMode::Truncate,
            RoundingMode::Floor,
            RoundingMode::Ceiling,
        ] {
            assert_eq!(a.round_to(mode, 18).unwrap(), a);
            assert_eq!(
                a.round_to(mode, 0).unwrap().to_i128(mode).unwrap(),
                a.to_i128(mode).unwrap(),
                "round_to(0) agrees with to_i128"
            );
        }
    }
}

#[test]
fn property_display_parse_round_trip() {
    let mut rng = Rng(0xDEAD_BEEF_CAFE_F00D);
    for _ in 0..2_000 {
        let value = random_decimal(&mut rng);
        let text = value.to_string();
        assert_eq!(
            Decimal18::from_str(&text).unwrap(),
            value,
            "display/parse round trip for {text}"
        );
        assert_eq!(Decimal18::from_unscaled(value.unscaled()).unwrap(), value);
    }
}

#[test]
fn property_ordering_is_total() {
    let mut rng = Rng(0x0123_4567_89AB_CDEF);
    for _ in 0..2_000 {
        let a = random_decimal(&mut rng);
        let b = random_decimal(&mut rng);
        match a.cmp(&b) {
            core::cmp::Ordering::Less => assert!(a.less_than(b)),
            core::cmp::Ordering::Equal => assert_eq!(a, b),
            core::cmp::Ordering::Greater => assert!(b.less_than(a)),
        }
    }
}

#[test]
fn one_unscaled_matches_scale() {
    assert_eq!(ONE_UNSCALED, 10i128.pow(18));
    assert_eq!(Decimal18::ONE.unscaled(), ONE_UNSCALED);
}
