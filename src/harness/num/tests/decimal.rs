//! The one-way `Decimal` to `f64` boundary used by metrics and statistics.

use core::str::FromStr;

use num::{Decimal, DecimalError};

fn decimal(text: &str) -> Decimal {
    Decimal::from_str(text).expect("valid decimal literal")
}

#[test]
fn fixed_conversion_matches_checked_text_rounding_and_overflow() {
    for text in [
        "0",
        "-0",
        "0E-900",
        "1.5",
        "1.50E+2",
        "-999.001",
        "1.0000000000000000005",
        "1.0000000000000000015",
        "-1.0000000000000000015",
        "1E-99",
        "1E+30",
        "99999999999999999999.999999999999999999",
    ] {
        assert_eq!(
            num::Decimal18::from_decimal(decimal(text)),
            text.parse::<num::Decimal18>(),
            "{text}"
        );
    }
}

#[test]
fn exact_values_convert_to_the_nearest_float() {
    assert_eq!(decimal("1.5").to_f64(), 1.5);
    assert_eq!(decimal("150").to_f64(), 150.0);
    assert_eq!(decimal("-2.25").to_f64(), -2.25);
    assert_eq!(Decimal::from_i128(42).to_f64(), 42.0);
    assert_eq!(Decimal::ZERO.to_f64(), 0.0);
    assert_eq!(Decimal::ONE.to_f64(), 1.0);
}

#[test]
fn scale_and_exponent_forms_convert_identically() {
    assert_eq!(decimal("1.500").to_f64(), decimal("1.5").to_f64());
    assert_eq!(decimal("1.50E+2").to_f64(), 150.0);
    assert_eq!(decimal("150E-1").to_f64(), 15.0);
    assert_eq!(decimal("1E+2").to_f64(), 100.0);
    assert_eq!(decimal("1E-2").to_f64(), 0.01);
}

#[test]
fn ties_round_to_the_even_neighbor() {
    // 2^53 has a two-unit spacing, so these odd literals are exact midpoints.
    assert_eq!(decimal("9007199254740993").to_f64(), 9007199254740992.0);
    assert_eq!(decimal("9007199254740995").to_f64(), 9007199254740996.0);
    assert_eq!(decimal("-9007199254740993").to_f64(), -9007199254740992.0);
}

#[test]
fn negative_zero_keeps_its_sign() {
    let negative = decimal("-0");
    assert_eq!(negative.to_f64(), 0.0);
    assert!(negative.to_f64().is_sign_negative());
    assert!(!decimal("0").to_f64().is_sign_negative());
}

#[test]
fn magnitudes_beyond_the_float_range_saturate_or_fail_checked() {
    let huge = Decimal::from_parts(false, 1, -400);
    assert_eq!(huge.to_f64(), f64::INFINITY);
    assert_eq!(huge.to_f64_checked(), Err(DecimalError::Overflow));
    assert_eq!(f64::try_from(huge), Err(DecimalError::Overflow));

    let tiny_negative = Decimal::from_parts(true, 1, -400);
    assert_eq!(tiny_negative.to_f64(), f64::NEG_INFINITY);
    assert_eq!(tiny_negative.to_f64_checked(), Err(DecimalError::Overflow));

    let underflow = Decimal::from_parts(false, 1, 400);
    assert_eq!(underflow.to_f64(), 0.0);
    assert_eq!(underflow.to_f64_checked(), Ok(0.0));
    assert_eq!(f64::try_from(underflow), Ok(0.0));
}

#[test]
fn try_from_matches_the_checked_method_for_finite_values() {
    for text in ["0", "1.5", "-1.5", "123456789.987654321", "1E-300"] {
        let value = decimal(text);
        assert_eq!(f64::try_from(value), value.to_f64_checked());
    }
}
