use super::U256;

#[test]
fn multiplication_matches_reference_limb_math() {
    let product = U256::mul_u128(u128::MAX, u128::MAX);
    assert_eq!(product.high, u128::MAX - 1);
    assert_eq!(product.low, 1);
    assert_eq!(U256::mul_u128(1, 1), U256 { high: 0, low: 1 });
    assert_eq!(U256::mul_u128(u128::MAX, 0), U256::ZERO);
}

#[test]
fn division_round_trips() {
    let numerator = U256::mul_u128(1_000_000_000_000_000_000, 7);
    let (quotient, remainder) = U256::div_rem(numerator, U256::from_u128(7));
    assert_eq!(quotient, U256::from_u128(1_000_000_000_000_000_000));
    assert!(remainder.is_zero());
}

#[test]
fn checked_add_reports_overflow() {
    let max = U256 {
        high: u128::MAX,
        low: u128::MAX,
    };
    assert!(max.checked_add(U256::from_u128(1)).is_none());
}
