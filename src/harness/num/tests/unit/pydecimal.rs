use super::PyDecimal;
use core::str::FromStr;

fn dec(text: &str) -> PyDecimal {
    PyDecimal::from_str(text).expect("valid decimal")
}

#[test]
fn parse_preserves_scale() {
    assert_eq!(dec("100").to_string(), "100");
    assert_eq!(dec("1.500").to_string(), "1.500");
    assert_eq!(dec("1E+2").to_string(), "1E+2");
    assert_eq!(dec("0.00").to_string(), "0.00");
    assert_eq!(dec("-0").to_string(), "-0");
    assert_eq!(dec(" 1.5 ").to_string(), "1.5");
    assert_eq!(dec(".5").to_string(), "0.5");
    assert_eq!(dec("5.").to_string(), "5");
    assert_eq!(dec("00100").to_string(), "100");
}

#[test]
fn arithmetic_scale_rules() {
    assert_eq!((dec("1.50") + dec("2")).to_string(), "3.50");
    assert_eq!((dec("100") * dec("0.01")).to_string(), "1.00");
    assert_eq!((dec("100") / dec("0.01")).to_string(), "1.00E+4");
    assert_eq!(
        (dec("1") / dec("3")).to_string(),
        "0.3333333333333333333333333333"
    );
    assert_eq!((dec("1.5") * dec("2")).to_string(), "3.0");
}

#[test]
fn equality_is_numeric() {
    assert_eq!(dec("1.0"), dec("1.00"));
    assert_eq!(dec("100"), dec("1E+2"));
    assert_eq!(dec("-0"), dec("0"));
    assert!(dec("1.0") < dec("1.01"));
}

#[test]
fn normalization_and_integral_value() {
    assert_eq!(
        dec("100")
            .normalize_with(&Default::default())
            .unwrap()
            .to_string(),
        "1E+2"
    );
    assert_eq!(
        dec("1.500")
            .normalize_with(&Default::default())
            .unwrap()
            .to_string(),
        "1.5"
    );
    assert_eq!(
        dec("2.5")
            .to_integral_value(&Default::default())
            .to_string(),
        "2"
    );
    assert_eq!(
        dec("-2.5")
            .to_integral_value(&Default::default())
            .to_string(),
        "-2"
    );
    assert_eq!(
        dec("1E+2")
            .to_integral_value(&Default::default())
            .to_string(),
        "1E+2"
    );
}
