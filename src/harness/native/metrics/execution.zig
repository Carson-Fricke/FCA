//! Execution aggregates: per-fill slippage, latency, order outcome
//! counts, and the combined execution stats. Tests are colocated and
//! pin the hand-computed vectors from
//! ``tests/harness/results/test_metrics.py`` (including the
//! slippage-to-ledger reconciliation).

const std = @import("std");
const common = @import("common.zig");

const dec = common.decimal.dec;
const FillRow = common.FillRow;
const OrderRow = common.OrderRow;
const LatencyStats = common.LatencyStats;
const OrderOutcomeCounts = common.OrderOutcomeCounts;
const ExecutionStats = common.ExecutionStats;
const MetricsError = common.MetricsError;

/// Signed slippage of one fill against its reference price: buys carry
/// positive quantity and sells negative quantity, so favorable sells
/// still contribute positive slippage cost when the fill price lands
/// below the reference.
pub fn fillSlippage(quantity: i128, reference_price: i128, fill_price: i128) MetricsError!i128 {
    return dec.mul(quantity, try dec.sub(fill_price, reference_price));
}

/// Aggregate fill latencies, ignoring null-latency rows.
pub fn latencyStats(fills: []const FillRow) MetricsError!LatencyStats {
    var count: usize = 0;
    var sum: i128 = 0;
    var max_ns: ?i64 = null;
    for (fills) |fill_row| {
        const latency = fill_row.latency_ns orelse continue;
        count += 1;
        sum = try dec.add(sum, @as(i128, latency) * common.one);
        max_ns = if (max_ns) |current| @max(current, latency) else latency;
    }
    if (count == 0) return .{ .count = 0, .mean_ns = null, .max_ns = null };
    return .{
        .count = count,
        .mean_ns = try dec.div(sum, @as(i128, count) * common.one),
        .max_ns = max_ns,
    };
}

/// Count order outcomes by exact lowercase classification.
pub fn orderOutcomeCounts(orders: []const OrderRow) OrderOutcomeCounts {
    var counts = OrderOutcomeCounts{
        .total = orders.len,
        .filled = 0,
        .rejected = 0,
        .cancelled = 0,
        .other = 0,
    };
    for (orders) |order| {
        if (std.mem.eql(u8, order.outcome, "filled")) {
            counts.filled += 1;
        } else if (std.mem.eql(u8, order.outcome, "rejected")) {
            counts.rejected += 1;
        } else if (std.mem.eql(u8, order.outcome, "cancelled")) {
            counts.cancelled += 1;
        } else {
            counts.other += 1;
        }
    }
    return counts;
}

/// Aggregate fees, slippage, latency, and outcomes over the ledgers.
pub fn executionStats(fills: []const FillRow, orders: []const OrderRow) MetricsError!ExecutionStats {
    var fees_total = common.zero;
    var slippage_total = common.zero;
    for (fills) |fill_row| {
        fees_total = try dec.add(fees_total, fill_row.fee);
        slippage_total = try dec.add(slippage_total, fill_row.slippage);
    }
    return .{
        .fill_count = fills.len,
        .fees_total = fees_total,
        .slippage_total = slippage_total,
        .latency = try latencyStats(fills),
        .outcomes = orderOutcomeCounts(orders),
    };
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/results/test_metrics.py.
// ---------------------------------------------------------------------------

const decText = common.decText;
const testing = std.testing;

fn fill(latency_ns: ?i64, fee: i128, slippage: i128) FillRow {
    return .{
        .quantity = decText("1"),
        .price = decText("100"),
        .fee = fee,
        .slippage = slippage,
        .latency_ns = latency_ns,
    };
}

test "slippage is the signed quantity times the price offset" {
    try testing.expectEqual(decText("1.0"), try fillSlippage(decText("2"), decText("100"), decText("100.5")));
    try testing.expectEqual(decText("1.5"), try fillSlippage(decText("-3"), decText("200"), decText("199.5")));
    // A favorable sell still contributes positive cost.
    try testing.expectEqual(decText("0.5"), try fillSlippage(decText("-1"), decText("100"), decText("99.5")));
}

test "latency stats skip null rows and aggregate the rest" {
    const fills = [_]FillRow{
        fill(100, decText("0"), decText("0")),
        fill(200, decText("0"), decText("0")),
        fill(null, decText("0"), decText("0")),
        fill(300, decText("0"), decText("0")),
    };
    const stats = try latencyStats(&fills);
    try testing.expectEqual(@as(usize, 3), stats.count);
    try testing.expectEqual(decText("200"), stats.mean_ns.?);
    try testing.expectEqual(@as(i64, 300), stats.max_ns.?);
    const none = [_]FillRow{fill(null, decText("0"), decText("0"))};
    const empty = try latencyStats(&none);
    try testing.expectEqual(@as(usize, 0), empty.count);
    try testing.expectEqual(@as(?i128, null), empty.mean_ns);
    try testing.expectEqual(@as(?i64, null), empty.max_ns);
}

test "order outcome counts classify the exact lowercase strings" {
    const orders = [_]OrderRow{
        .{ .outcome = "filled" },
        .{ .outcome = "filled" },
        .{ .outcome = "rejected" },
        .{ .outcome = "cancelled" },
        .{ .outcome = "expired" },
    };
    const counts = orderOutcomeCounts(&orders);
    try testing.expectEqual(@as(usize, 5), counts.total);
    try testing.expectEqual(@as(usize, 2), counts.filled);
    try testing.expectEqual(@as(usize, 1), counts.rejected);
    try testing.expectEqual(@as(usize, 1), counts.cancelled);
    try testing.expectEqual(@as(usize, 1), counts.other);
    const empty = orderOutcomeCounts(&[_]OrderRow{});
    try testing.expectEqual(@as(usize, 0), empty.total);
}

test "execution stats aggregate fees, slippage, latency, and outcomes" {
    const fills = [_]FillRow{
        fill(10, decText("1.5"), decText("1.0")),
        fill(30, decText("2.5"), decText("0.2")),
    };
    const orders = [_]OrderRow{ .{ .outcome = "filled" }, .{ .outcome = "rejected" } };
    const stats = try executionStats(&fills, &orders);
    try testing.expectEqual(@as(usize, 2), stats.fill_count);
    try testing.expectEqual(decText("4.0"), stats.fees_total);
    try testing.expectEqual(decText("1.2"), stats.slippage_total);
    try testing.expectEqual(decText("20"), stats.latency.mean_ns.?);
    try testing.expectEqual(@as(usize, 2), stats.outcomes.total);
    const none = try executionStats(&[_]FillRow{}, &[_]OrderRow{});
    try testing.expectEqual(@as(usize, 0), none.fill_count);
    try testing.expectEqual(decText("0"), none.fees_total);
    try testing.expectEqual(@as(?i128, null), none.latency.mean_ns);
}

test "slippage reconciliation matches the ledger cost components exactly" {
    const ledger = [_]struct { quantity: i128, reference: i128, fill_price: i128 }{
        .{ .quantity = decText("2"), .reference = decText("100"), .fill_price = decText("100.5") },
        .{ .quantity = decText("1"), .reference = decText("50"), .fill_price = decText("50.4") },
        .{ .quantity = decText("-3"), .reference = decText("200"), .fill_price = decText("199.5") },
    };
    var total = common.zero;
    var rows: [ledger.len]FillRow = undefined;
    for (ledger, 0..) |row, index| {
        const slip = try fillSlippage(row.quantity, row.reference, row.fill_price);
        rows[index] = .{
            .quantity = row.quantity,
            .price = row.fill_price,
            .fee = decText("0"),
            .slippage = slip,
            .latency_ns = null,
        };
        total = try dec.add(total, slip);
    }
    try testing.expectEqual(decText("1.0"), rows[0].slippage);
    try testing.expectEqual(decText("1.5"), rows[2].slippage);
    const stats = try executionStats(&rows, &[_]OrderRow{});
    try testing.expectEqual(total, stats.slippage_total);
}
