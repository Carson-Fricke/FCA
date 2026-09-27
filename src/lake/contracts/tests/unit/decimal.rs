use super::*;

#[test]
fn parses_exact_literals_without_rounding() {
    let value = Decimal18::parse("123.45").expect("parse");
    assert_eq!(value.unscaled(), 123_450_000_000_000_000_000);
    assert_eq!(value.to_canonical_string(), "123.45");
    assert_eq!(
        Decimal18::parse("1.5e-3")
            .expect("exponent")
            .to_canonical_string(),
        "0.0015"
    );
    assert_eq!(
        Decimal18::parse("-0.000000000000000001")
            .expect("tiny")
            .unscaled(),
        -1
    );
    assert_eq!(
        Decimal18::parse(" 5.25 ").expect("spaces").to_string(),
        "5.25"
    );
}

#[test]
fn rejects_invalid_and_over_precise_literals() {
    assert!(Decimal18::parse("").is_err());
    assert!(Decimal18::parse("abc").is_err());
    assert!(Decimal18::parse("1.2.3").is_err());
    assert!(Decimal18::parse("1e").is_err());
    assert!(Decimal18::parse("0.0000000000000000001").is_err());
}

#[test]
fn range_checks_decimal128_38_18() {
    assert!(Decimal18::from_unscaled(MAX_UNSCALED).is_ok());
    assert!(Decimal18::from_unscaled(MAX_UNSCALED + 1).is_err());
    assert!(Decimal18::parse("100000000000000000000").is_err());
}

#[test]
fn orders_and_signs_report_exactly() {
    let low = Decimal18::parse("1.5").expect("low");
    let high = Decimal18::parse("2").expect("high");
    assert!(low < high);
    assert!(low.is_positive());
    assert!(Decimal18::parse("-1").expect("negative").is_negative());
    assert!(Decimal18::ZERO.is_zero());
}

#[test]
fn serde_uses_canonical_decimal_strings() {
    let value = Decimal18::parse("5.250").expect("value");
    let encoded = serde_json::to_string(&value).expect("encode");
    assert_eq!(encoded, "\"5.25\"");
    let decoded: Decimal18 = serde_json::from_str("\"5.25\"").expect("decode");
    assert_eq!(decoded, value);
    let numeric: Decimal18 = serde_json::from_str("42").expect("numeric decode");
    assert_eq!(numeric.to_string(), "42");
}
