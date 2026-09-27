//! The C ABI decimal crossing is lossless.
//!
//! Every engine decimal crosses the dylib boundary as a `CDecimal`; these
//! tests pin the exact roundtrip through the shapes that have actually
//! broken naive fixed-point ports: preserved trailing zeros, negative zero,
//! positive exponents, and full-width coefficients.

use harness_strategy_sdk::{CDecimal, Decimal};

fn roundtrip(text: &str) {
    let value: Decimal = text.parse().expect("decimal parses");
    let crossing = CDecimal::from_decimal(value);
    let restored = crossing.to_decimal();
    assert_eq!(
        restored.to_string(),
        value.to_string(),
        "display roundtrip for {text}"
    );
    assert_eq!(restored, value, "value roundtrip for {text}");
}

#[test]
fn decimal_roundtrip_preserves_scale_sign_and_zero() {
    roundtrip("1.500");
    roundtrip("450.01");
    roundtrip("0");
    roundtrip("-0");
    roundtrip("1E+2");
    roundtrip("-1E-8");
    roundtrip("100");
    roundtrip("0.000000001");
    roundtrip("-123.45678901234567890123456789012345678");
    roundtrip("340282366920938463463374607431768211455");
}
