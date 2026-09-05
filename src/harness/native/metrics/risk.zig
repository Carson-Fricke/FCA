//! Return-series risk metrics: annualized Sharpe and Sortino ratios on
//! the pinned scale-18 kernel, including the correctly rounded scale-18
//! square root. Tests are colocated and pin the hand-computed vectors
//! from ``tests/harness/results/test_metrics.py``.

const std = @import("std");
const common = @import("common.zig");

const dec = common.decimal.dec;
const MetricsError = common.MetricsError;

/// Scale-18 half-even square root: ``sqrt(unscaled * 10^18)`` rounded
/// to nearest, ties to even, mirroring Python's correctly rounded
/// ``Decimal.sqrt()`` within the pinned scale.
pub fn decSqrt(value: i128) MetricsError!i128 {
    if (value < 0) return error.InvalidInput;
    if (value == 0) return 0;
    const wide: i256 = @as(i256, value) * dec.one;
    var high: i256 = 1;
    while (high * high < wide) high *= 2;
    var low: i256 = 0;
    while (low < high) {
        const mid = low + @divTrunc(high - low + 1, 2);
        if (mid * mid <= wide) {
            low = mid;
        } else {
            high = mid - 1;
        }
    }
    const below = wide - low * low;
    const above = (low + 1) * (low + 1) - wide;
    var root = low;
    if (above < below) {
        root = low + 1;
    } else if (above == below and (low & 1) == 1) {
        root = low + 1;
    }
    if (root > @as(i256, dec.max_unscaled)) return error.OutOfRange;
    return @intCast(root);
}

/// Annualized Sharpe ratio: ``mean / population_std * sqrt(ppy)`` with
/// the standard deviation over the same sample (ddof=0). ``null`` for
/// fewer than two returns, a non-positive ``ppy``, or zero variance.
pub fn sharpe(returns: []const i128, periods_per_year: u32) MetricsError!?f64 {
    if (returns.len < 2 or periods_per_year == 0) return null;
    const count = @as(i128, returns.len) * common.one;
    var sum = common.zero;
    for (returns) |value| sum = try dec.add(sum, value);
    const mean = try dec.div(sum, count);
    var variance_sum = common.zero;
    for (returns) |value| {
        const deviation = try dec.sub(value, mean);
        variance_sum = try dec.add(variance_sum, try dec.mul(deviation, deviation));
    }
    const variance = try dec.div(variance_sum, count);
    if (variance == common.zero) return null;
    const std_dev = try decSqrt(variance);
    const ratio = try dec.div(mean, std_dev);
    return common.unscaledToF64(ratio) * @sqrt(@as(f64, @floatFromInt(periods_per_year)));
}

/// Annualized Sortino ratio: ``mean / downside_deviation * sqrt(ppy)``
/// where the downside deviation is the square root of the mean squared
/// shortfall below ``target`` over all periods. ``null`` when no return
/// falls strictly below the target.
pub fn sortino(returns: []const i128, periods_per_year: u32, target: i128) MetricsError!?f64 {
    if (returns.len == 0 or periods_per_year == 0) return null;
    var sum = common.zero;
    var variance_sum = common.zero;
    var shortfalls: usize = 0;
    for (returns) |value| {
        sum = try dec.add(sum, value);
        if (value < target) {
            const shortfall = try dec.sub(value, target);
            variance_sum = try dec.add(variance_sum, try dec.mul(shortfall, shortfall));
            shortfalls += 1;
        }
    }
    if (shortfalls == 0) return null;
    const count = @as(i128, returns.len) * common.one;
    const mean = try dec.div(sum, count);
    const variance = try dec.div(variance_sum, count);
    if (variance == common.zero) return null;
    const std_dev = try decSqrt(variance);
    const ratio = try dec.div(mean, std_dev);
    return common.unscaledToF64(ratio) * @sqrt(@as(f64, @floatFromInt(periods_per_year)));
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/results/test_metrics.py.
// ---------------------------------------------------------------------------

const decText = common.decText;
const testing = std.testing;

fn expectApprox(actual: f64, expected: f64) !void {
    try testing.expectApproxEqRel(expected, actual, 1e-12);
}

test "sharpe pins the hand-computed vector and undefined cases" {
    const returns = [_]i128{ decText("0.05"), decText("0.05"), decText("-0.01"), decText("-0.01") };
    const expected = (2.0 / 3.0) * @sqrt(@as(f64, 252.0));
    try expectApprox((try sharpe(&returns, 252)).?, expected);
    const zero_variance = [_]i128{ decText("0.02"), decText("0.02") };
    try testing.expectEqual(@as(?f64, null), try sharpe(&zero_variance, 252));
    const single = [_]i128{decText("0.02")};
    try testing.expectEqual(@as(?f64, null), try sharpe(&single, 252));
    try testing.expectEqual(@as(?f64, null), try sharpe(&[_]i128{}, 252));
}

test "sortino pins the hand-computed vector and no-downside cases" {
    const returns = [_]i128{ decText("0.03"), decText("-0.01"), decText("0.05"), decText("-0.03") };
    const mean = 0.01;
    const downside = @sqrt((0.0001 + 0.0009) / 4.0);
    try expectApprox((try sortino(&returns, 4, decText("0"))).?, mean / downside * 2.0);
    const no_downside = [_]i128{ decText("0.01"), decText("0.02") };
    try testing.expectEqual(@as(?f64, null), try sortino(&no_downside, 4, decText("0")));
    const all_zero = [_]i128{ decText("0"), decText("0") };
    try testing.expectEqual(@as(?f64, null), try sortino(&all_zero, 4, decText("0")));
}

test "scale-18 square root rounds half-even against exact squares" {
    try testing.expectEqual(@as(i128, 300_000_000_000_000_000), try decSqrt(decText("0.09")));
    try testing.expectEqual(@as(i128, common.one), try decSqrt(common.one));
    try testing.expectEqual(@as(i128, 0), try decSqrt(0));
    try testing.expectError(error.InvalidInput, decSqrt(-1));
    // Ties round to even: sqrt(2.25) = 1.5 exactly, sqrt of a
    // non-square lands on the nearest representable neighbor.
    try testing.expectEqual(@as(i128, 1_500_000_000_000_000_000), try decSqrt(decText("2.25")));
}
