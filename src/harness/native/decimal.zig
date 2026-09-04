//! Pinned fixed-point decimal kernel shared by the OMS, books, and market
//! modules (previously ``oms.dec``).
//!
//! Unscaled values are i128 at scale 18 (``decimal128(38, 18)``, the
//! repository's binary decimal convention); every operation is range-checked
//! against decimal128(38, 18) and never rounds on exact inputs. ``mul`` and
//! ``div`` round half-even at scale 18 when the exact result is not
//! representable (Python rounds Decimal arithmetic to 28 significant digits;
//! values crossing the native boundary are pinned to scale 18).

const std = @import("std");

pub const dec = struct {
    pub const scale: u32 = 18;
    /// 10^18 unscaled units per whole unit.
    pub const one: i128 = 1_000_000_000_000_000_000;
    /// Largest magnitude representable by decimal128(38, 18): 10^38 - 1.
    pub const max_unscaled: i128 = 99_999_999_999_999_999_999_999_999_999_999_999_999;

    pub const DecimalError = error{
        /// Fixed-point overflow or a non-representable value.
        OutOfRange,
        /// Division by zero.
        InvalidDecimal,
    };

    const one_wide: i256 = one;
    const bound_wide: i256 = max_unscaled;

    pub fn fromInt(value: i64) i128 {
        return @as(i128, value) * one;
    }

    pub fn narrow(value: i256) DecimalError!i128 {
        if (value > bound_wide or value < -bound_wide) return error.OutOfRange;
        return @intCast(value);
    }

    pub fn add(a: i128, b: i128) DecimalError!i128 {
        return narrow(@as(i256, a) + @as(i256, b));
    }

    pub fn sub(a: i128, b: i128) DecimalError!i128 {
        return narrow(@as(i256, a) - @as(i256, b));
    }

    /// Multiply two scale-18 values; exact when the product has at most 18
    /// fractional digits, otherwise rounded half-even at scale 18.
    pub fn mul(a: i128, b: i128) DecimalError!i128 {
        return narrow(divRoundHalfEven(@as(i256, a) * @as(i256, b), one_wide));
    }

    /// Multiply then divide in one step: ``a * b / c`` rounded half-even at
    /// scale 18, avoiding a double rounding.
    pub fn mulDiv(a: i128, b: i128, c: i128) DecimalError!i128 {
        if (c == 0) return error.InvalidDecimal;
        return narrow(divRoundHalfEven(
            @as(i256, a) * @as(i256, b),
            @as(i256, c),
        ));
    }

    /// Divide ``a`` by ``b`` at scale 18, rounding half-even.
    pub fn div(a: i128, b: i128) DecimalError!i128 {
        if (b == 0) return error.InvalidDecimal;
        return narrow(divRoundHalfEven(@as(i256, a) * one_wide, @as(i256, b)));
    }

    fn divRoundHalfEven(numerator: i256, denominator: i256) i256 {
        const negative = (numerator < 0) != (denominator < 0);
        const mag_numerator: u256 = @abs(numerator);
        const mag_denominator: u256 = @abs(denominator);
        var quotient: u256 = mag_numerator / mag_denominator;
        const remainder: u256 = mag_numerator % mag_denominator;
        const twice = remainder * 2;
        if (twice > mag_denominator or (twice == mag_denominator and (quotient & 1) == 1)) {
            quotient += 1;
        }
        if (negative) return -@as(i256, @intCast(quotient));
        return @intCast(quotient);
    }
};

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror the exact decimal arithmetic pinned by
// tests/harness/oms/test_state.py and the portfolio book tests.
// ---------------------------------------------------------------------------

const testing = std.testing;

test "fixed-point kernel matches exact decimal arithmetic" {
    // 0.125 * 10.01 == 1.25125 and 5.0125 / 0.500 == 10.025 (exact).
    try testing.expectEqual(
        @as(i128, 1_251_250_000_000_000_000),
        try dec.mul(125_000_000_000_000_000, 10_010_000_000_000_000_000),
    );
    try testing.expectEqual(
        @as(i128, 10_025_000_000_000_000_000),
        try dec.div(5_012_500_000_000_000_000, 500_000_000_000_000_000),
    );
    // 930 / 9 rounds half-even at scale 18.
    try testing.expectEqual(
        @as(i128, 103_333_333_333_333_333_333),
        try dec.div(930_000_000_000_000_000_000, dec.fromInt(9)),
    );
    // Ties round to even: 1e-18 / 2 -> 0, 3e-18 / 2 -> 2e-18.
    try testing.expectEqual(@as(i128, 0), try dec.div(1, dec.fromInt(2)));
    try testing.expectEqual(@as(i128, 2), try dec.div(3, dec.fromInt(2)));
    // 1.5 / 2 == 0.75 exactly.
    try testing.expectEqual(
        @as(i128, 750_000_000_000_000_000),
        try dec.div(1_500_000_000_000_000_000, dec.fromInt(2)),
    );
    try testing.expectError(error.InvalidDecimal, dec.div(dec.one, 0));
    try testing.expectError(error.OutOfRange, dec.mul(dec.max_unscaled, dec.fromInt(2)));
}
