use super::{add, div, from_int, mul, mul_div, sub, DecimalError, MAX_UNSCALED, ONE};

#[test]
fn fixed_point_kernel_matches_exact_decimal_arithmetic() {
    assert_eq!(
        mul(125_000_000_000_000_000, 10_010_000_000_000_000_000),
        Ok(1_251_250_000_000_000_000)
    );
    assert_eq!(
        div(5_012_500_000_000_000_000, 500_000_000_000_000_000),
        Ok(10_025_000_000_000_000_000)
    );
    assert_eq!(
        div(930_000_000_000_000_000_000, from_int(9)),
        Ok(103_333_333_333_333_333_333)
    );
    assert_eq!(div(1, from_int(2)), Ok(0));
    assert_eq!(div(3, from_int(2)), Ok(2));
    assert_eq!(
        div(1_500_000_000_000_000_000, from_int(2)),
        Ok(750_000_000_000_000_000)
    );
    assert_eq!(div(ONE, 0), Err(DecimalError::InvalidDecimal));
    assert_eq!(
        mul(MAX_UNSCALED, from_int(2)),
        Err(DecimalError::OutOfRange)
    );
}

#[test]
fn add_sub_and_mul_div_match_zig_semantics() {
    assert_eq!(add(ONE, ONE), Ok(2 * ONE));
    assert_eq!(sub(0, ONE), Ok(-ONE));
    assert_eq!(add(MAX_UNSCALED, 1), Err(DecimalError::OutOfRange));
    assert_eq!(
        mul_div(from_int(3), ONE, from_int(2)),
        Ok(1_500_000_000_000_000_000)
    );
    assert_eq!(mul_div(ONE, ONE, 0), Err(DecimalError::InvalidDecimal));
}

#[test]
fn matches_num_for_in_range_values() {
    use num::Decimal18;

    let cases: [(i128, i128); 8] = [
        (0, 0),
        (ONE, ONE),
        (-ONE, 3 * ONE),
        (125_000_000_000_000_000, 10_010_000_000_000_000_000),
        (5_012_500_000_000_000_000, 500_000_000_000_000_000),
        (930_000_000_000_000_000_000, 9 * ONE),
        (1, 3),
        (99_999_999_999_999_999_999, 3),
    ];
    for (a, b) in cases {
        let lhs = Decimal18::from_unscaled(a);
        let rhs = Decimal18::from_unscaled(b);
        if let (Ok(lhs), Ok(rhs)) = (lhs, rhs) {
            assert_eq!(
                mul(a, b).ok(),
                lhs.checked_mul(rhs).ok().map(Decimal18::unscaled)
            );
            assert_eq!(
                sub(a, b).ok(),
                lhs.checked_sub(rhs).ok().map(Decimal18::unscaled)
            );
            if b != 0 {
                assert_eq!(
                    div(a, b).ok(),
                    lhs.checked_div(rhs).ok().map(Decimal18::unscaled)
                );
                assert_eq!(
                    mul_div(a, b, rhs.unscaled()).ok(),
                    lhs.checked_mul_div(rhs, rhs).ok().map(Decimal18::unscaled)
                );
            }
        }
    }
}
