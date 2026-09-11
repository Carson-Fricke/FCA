use super::{Decimal18, DecimalError};
use core::str::FromStr;

#[test]
fn from_unscaled_enforces_decimal128_range() {
    assert!(Decimal18::from_unscaled(Decimal18::MAX.unscaled()).is_ok());
    assert_eq!(
        Decimal18::from_unscaled(Decimal18::MAX.unscaled() + 1),
        Err(DecimalError::Overflow)
    );
    assert_eq!(
        Decimal18::from_unscaled(Decimal18::MIN.unscaled() - 1),
        Err(DecimalError::Overflow)
    );
}

#[test]
fn exact_mul_and_div_do_not_round() {
    let a = Decimal18::from_str("0.125").unwrap();
    let b = Decimal18::from_str("10.01").unwrap();
    assert_eq!((a * b).to_string(), "1.251250000000000000");
    let c = Decimal18::from_str("5.0125").unwrap();
    let d = Decimal18::from_str("0.500").unwrap();
    assert_eq!((c / d).to_string(), "10.025000000000000000");
}

#[test]
fn ties_round_half_even() {
    let third = Decimal18::from_str("930").unwrap() / Decimal18::from_str("9").unwrap();
    assert_eq!(third.to_string(), "103.333333333333333333");
    assert_eq!(
        (Decimal18::from_unscaled(1).unwrap() / Decimal18::from(2)).unscaled(),
        0
    );
    assert_eq!(
        (Decimal18::from_unscaled(3).unwrap() / Decimal18::from(2)).unscaled(),
        2
    );
}

#[test]
fn display_matches_python_notation() {
    assert_eq!(Decimal18::ZERO.to_string(), "0E-18");
    assert_eq!(Decimal18::from_unscaled(1).unwrap().to_string(), "1E-18");
    assert_eq!(Decimal18::ONE.to_string(), "1.000000000000000000");
    assert_eq!(
        Decimal18::from_unscaled(1_000_000_000_000)
            .unwrap()
            .to_string(),
        "0.000001000000000000"
    );
}
