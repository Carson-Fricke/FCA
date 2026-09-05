//! Equity-curve metrics: total return, 365-day-year CAGR, max drawdown,
//! and the longest peak-to-recovery span. Tests are colocated and pin
//! the hand-computed vectors from ``tests/harness/results/test_metrics.py``.

const std = @import("std");
const common = @import("common.zig");

const dec = common.decimal.dec;
const EquityPoint = common.EquityPoint;
const DrawdownSpan = common.DrawdownSpan;
const EquityMetrics = common.EquityMetrics;
const MetricsError = common.MetricsError;

fn validatePoints(points: []const EquityPoint) MetricsError!void {
    for (points, 0..) |point, index| {
        if (point.equity < common.zero) return error.InvalidInput;
        if (index > 0 and point.timestamp_ns < points[index - 1].timestamp_ns) {
            return error.InvalidInput;
        }
    }
}

/// ``last/first - 1`` over the curve; ``null`` for fewer than two
/// points or a zero first equity.
pub fn totalReturn(points: []const EquityPoint) MetricsError!?i128 {
    try validatePoints(points);
    if (points.len < 2 or points[0].equity == common.zero) return null;
    return @as(?i128, try dec.sub(try dec.div(points[points.len - 1].equity, points[0].equity), common.one));
}

/// 365-day-year CAGR; ``null`` for fewer than two points, a
/// non-positive span or start equity, or a non-positive growth ratio
/// (a total wipeout is reported by the drawdown instead).
pub fn cagr(points: []const EquityPoint) MetricsError!?f64 {
    try validatePoints(points);
    if (points.len < 2) return null;
    const start = points[0];
    const end = points[points.len - 1];
    const span = end.timestamp_ns - start.timestamp_ns;
    if (span <= 0 or start.equity <= common.zero) return null;
    const growth = try dec.div(end.equity, start.equity);
    if (growth <= common.zero) return null;
    const years = try dec.div(
        @as(i128, span) * common.one,
        @as(i128, common.nanos_per_year_365) * common.one,
    );
    const exponent = try dec.div(common.one, years);
    return std.math.pow(
        f64,
        common.unscaledToF64(growth),
        common.unscaledToF64(exponent),
    );
}

/// Largest ``1 - trough/peak`` against the running peak; ``0`` when the
/// curve never falls below its running peak (and for single points).
pub fn maxDrawdown(points: []const EquityPoint) MetricsError!i128 {
    try validatePoints(points);
    var peak = if (points.len > 0) points[0].equity else common.zero;
    var worst = common.zero;
    for (points) |point| {
        if (point.equity > peak) {
            peak = point.equity;
        } else if (peak > common.zero and point.equity < peak) {
            const drawdown = try dec.sub(common.one, try dec.div(point.equity, peak));
            if (drawdown > worst) worst = drawdown;
        }
    }
    return worst;
}

/// Longest peak-to-recovery span; ties prefer the recovered span. A
/// single-point curve and a curve that never dips report a zero-length
/// recovered span; an all-loss ending reports the open span from the
/// last peak with ``recovered = false``.
pub fn longestDrawdown(points: []const EquityPoint) MetricsError!DrawdownSpan {
    try validatePoints(points);
    if (points.len < 2) return .{ .duration_ns = 0, .recovered = true };
    var peak = points[0].equity;
    var peak_ts = points[0].timestamp_ns;
    var best_closed: i64 = 0;
    var open_peak_ts: ?i64 = null;
    for (points[1..]) |point| {
        if (point.equity >= peak) {
            if (open_peak_ts) |open_ts| {
                best_closed = @max(best_closed, point.timestamp_ns - open_ts);
                open_peak_ts = null;
            }
            peak = point.equity;
            peak_ts = point.timestamp_ns;
        } else if (open_peak_ts == null) {
            open_peak_ts = peak_ts;
        }
    }
    if (open_peak_ts) |open_ts| {
        const open_span = points[points.len - 1].timestamp_ns - open_ts;
        if (open_span > best_closed) {
            return .{ .duration_ns = open_span, .recovered = false };
        }
    }
    return .{ .duration_ns = best_closed, .recovered = true };
}

/// Every equity-curve metric for one curve at once.
pub fn equityCurveMetrics(points: []const EquityPoint) MetricsError!EquityMetrics {
    return .{
        .total_return = try totalReturn(points),
        .cagr = try cagr(points),
        .max_drawdown = try maxDrawdown(points),
        .drawdown = try longestDrawdown(points),
    };
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/results/test_metrics.py.
// ---------------------------------------------------------------------------

const decText = common.decText;
const testing = std.testing;
const day: i64 = common.nanos_per_day;

fn pt(ts_ns: i64, equity: i128) EquityPoint {
    return .{ .timestamp_ns = ts_ns, .equity = equity };
}

fn expectApprox(actual: f64, expected: f64) !void {
    try testing.expectApproxEqRel(expected, actual, 1e-12);
}

test "total return matches the two-point hand-computed vector" {
    const curve = [_]EquityPoint{
        pt(0, decText("100")),
        pt(day, decText("80")),
        pt(2 * day, decText("121")),
    };
    try testing.expectEqual(decText("0.21"), (try totalReturn(&curve)).?);
    const single = [_]EquityPoint{pt(0, decText("100"))};
    try testing.expectEqual(@as(?i128, null), try totalReturn(&single));
    const zero_start = [_]EquityPoint{ pt(0, decText("0")), pt(day, decText("50")) };
    try testing.expectEqual(@as(?i128, null), try totalReturn(&zero_start));
    try testing.expectEqual(@as(?i128, null), try totalReturn(&[_]EquityPoint{}));
}

test "cagr compounds annually on the 365-day convention" {
    const year = [_]EquityPoint{ pt(0, decText("100")), pt(365 * day, decText("121")) };
    try expectApprox((try cagr(&year)).?, 1.21);
    const double = [_]EquityPoint{
        pt(0, decText("100")),
        pt(day, decText("105")),
        pt(365 * day, decText("121")),
    };
    try expectApprox((try cagr(&double)).?, 1.21);
    const half = [_]EquityPoint{ pt(0, decText("100")), pt(365 * day / 2, decText("121")) };
    try expectApprox((try cagr(&half)).?, 1.21 * 1.21);
}

test "cagr undefined cases return null" {
    const single = [_]EquityPoint{pt(0, decText("100"))};
    try testing.expectEqual(@as(?f64, null), try cagr(&single));
    const zero_start = [_]EquityPoint{ pt(0, decText("0")), pt(day, decText("10")) };
    try testing.expectEqual(@as(?f64, null), try cagr(&zero_start));
    const wipeout = [_]EquityPoint{ pt(0, decText("100")), pt(day, decText("0")) };
    try testing.expectEqual(@as(?f64, null), try cagr(&wipeout));
    const same_time = [_]EquityPoint{ pt(5, decText("100")), pt(5, decText("121")) };
    try testing.expectEqual(@as(?f64, null), try cagr(&same_time));
}

test "max drawdown pins the hand-computed and pathological vectors" {
    const curve = [_]EquityPoint{
        pt(0, decText("100")),
        pt(day, decText("80")),
        pt(2 * day, decText("120")),
        pt(3 * day, decText("90")),
    };
    try testing.expectEqual(decText("0.25"), try maxDrawdown(&curve));
    const rising = [_]EquityPoint{ pt(0, decText("100")), pt(day, decText("120")), pt(2 * day, decText("121")) };
    try testing.expectEqual(decText("0"), try maxDrawdown(&rising));
    const all_loss = [_]EquityPoint{ pt(0, decText("100")), pt(day, decText("50")), pt(2 * day, decText("0")) };
    try testing.expectEqual(decText("1"), try maxDrawdown(&all_loss));
    try testing.expectError(error.InvalidInput, maxDrawdown(&[_]EquityPoint{ pt(0, decText("100")), pt(day, decText("-1")) }));
    try testing.expectError(error.InvalidInput, maxDrawdown(&[_]EquityPoint{ pt(2, decText("100")), pt(1, decText("100")) }));
}

test "drawdown duration spans mirror the Python vectors" {
    const recovered = [_]EquityPoint{
        pt(0, decText("100")),
        pt(day, decText("80")),
        pt(2 * day, decText("120")),
        pt(3 * day, decText("90")),
    };
    var span = try longestDrawdown(&recovered);
    try testing.expectEqual(2 * day, span.duration_ns);
    try testing.expect(span.recovered);
    const open = [_]EquityPoint{ pt(0, decText("100")), pt(day, decText("50")), pt(2 * day, decText("0")) };
    span = try longestDrawdown(&open);
    try testing.expectEqual(2 * day, span.duration_ns);
    try testing.expect(!span.recovered);
    const longer_closed = [_]EquityPoint{ pt(0, decText("100")), pt(day, decText("90")), pt(2 * day, decText("100")), pt(3 * day, decText("99")) };
    span = try longestDrawdown(&longer_closed);
    try testing.expectEqual(2 * day, span.duration_ns);
    try testing.expect(span.recovered);
    const single = [_]EquityPoint{pt(0, decText("100"))};
    span = try longestDrawdown(&single);
    try testing.expect(span.recovered);
    const flat = [_]EquityPoint{ pt(0, decText("100")), pt(day, decText("100")), pt(2 * day, decText("100")) };
    span = try longestDrawdown(&flat);
    try testing.expectEqual(@as(i64, 0), span.duration_ns);
    try testing.expect(span.recovered);
}

test "the bundled equity metrics match the individual parts" {
    const curve = [_]EquityPoint{
        pt(0, decText("100")),
        pt(day, decText("80")),
        pt(2 * day, decText("120")),
        pt(3 * day, decText("90")),
    };
    const bundled = try equityCurveMetrics(&curve);
    try testing.expectEqual(try totalReturn(&curve), bundled.total_return);
    try testing.expectEqual(try maxDrawdown(&curve), bundled.max_drawdown);
    try testing.expectEqual(try longestDrawdown(&curve), bundled.drawdown);
}
