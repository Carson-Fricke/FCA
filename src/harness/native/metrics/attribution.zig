//! Per-strategy attribution: the trade and execution aggregates for
//! each strategy across the annotated ledgers. Tests are colocated and
//! pin the grouping vectors from ``tests/harness/results/test_metrics.py``.

const std = @import("std");
const common = @import("common.zig");

const AnnotatedFill = common.AnnotatedFill;
const AnnotatedOrder = common.AnnotatedOrder;
const AnnotatedTrade = common.AnnotatedTrade;
const StrategyAttribution = common.StrategyAttribution;
const AttributionError = common.AttributionError;

fn lessThanId(_: void, a: []const u8, b: []const u8) bool {
    return std.mem.lessThan(u8, a, b);
}

fn strategyIds(
    allocator: std.mem.Allocator,
    fills: []const AnnotatedFill,
    orders: []const AnnotatedOrder,
    trades: []const AnnotatedTrade,
) error{OutOfMemory}![][]const u8 {
    var seen = std.StringHashMap(void).init(allocator);
    defer seen.deinit();
    for (fills) |row| try seen.put(row.strategy_id, {});
    for (orders) |row| try seen.put(row.strategy_id, {});
    for (trades) |row| try seen.put(row.strategy_id, {});
    var ids = try std.ArrayList([]const u8).initCapacity(allocator, seen.count());
    defer ids.deinit(allocator);
    var iterator = seen.keyIterator();
    while (iterator.next()) |key| try ids.append(allocator, key.*);
    std.mem.sort([]const u8, ids.items, {}, lessThanId);
    return allocator.dupe([]const u8, ids.items);
}

/// Trade and execution aggregates for each strategy. Strategy IDs are
/// the sorted union of the strategies appearing in any input ledger; a
/// strategy present only in one ledger gets zeroed aggregates for the
/// others. The caller owns the returned slice (free with ``allocator``).
pub fn perStrategyAttribution(
    allocator: std.mem.Allocator,
    fills: []const AnnotatedFill,
    orders: []const AnnotatedOrder,
    trades: []const AnnotatedTrade,
) AttributionError![]StrategyAttribution {
    const ids = try strategyIds(allocator, fills, orders, trades);
    defer allocator.free(ids);
    const result = try allocator.alloc(StrategyAttribution, ids.len);
    errdefer allocator.free(result);
    for (ids, 0..) |id, index| {
        var strategy_fills: std.ArrayListUnmanaged(common.FillRow) = .empty;
        defer strategy_fills.deinit(allocator);
        var strategy_orders: std.ArrayListUnmanaged(common.OrderRow) = .empty;
        defer strategy_orders.deinit(allocator);
        var strategy_trades: std.ArrayListUnmanaged(common.TradeRow) = .empty;
        defer strategy_trades.deinit(allocator);
        for (fills) |row| {
            if (std.mem.eql(u8, row.strategy_id, id)) try strategy_fills.append(allocator, row.fill);
        }
        for (orders) |row| {
            if (std.mem.eql(u8, row.strategy_id, id)) try strategy_orders.append(allocator, row.order);
        }
        for (trades) |row| {
            if (std.mem.eql(u8, row.strategy_id, id)) try strategy_trades.append(allocator, row.trade);
        }
        result[index] = .{
            .strategy_id = id,
            .trades = try @import("trades.zig").tradeStats(strategy_trades.items),
            .execution = try @import("execution.zig").executionStats(strategy_fills.items, strategy_orders.items),
        };
    }
    return result;
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/results/test_metrics.py.
// ---------------------------------------------------------------------------

const decText = common.decText;
const testing = std.testing;

fn annotatedFill(strategy_id: []const u8, fee: i128, slippage: i128, latency_ns: ?i64) AnnotatedFill {
    return .{
        .strategy_id = strategy_id,
        .fill = .{
            .quantity = decText("1"),
            .price = decText("100"),
            .fee = fee,
            .slippage = slippage,
            .latency_ns = latency_ns,
        },
    };
}

test "per-strategy attribution groups every ledger and zeroes the absent ones" {
    const fills = [_]AnnotatedFill{
        annotatedFill("alpha", decText("1"), decText("0.5"), 10),
        annotatedFill("beta", decText("2"), decText("0"), 20),
    };
    const orders = [_]AnnotatedOrder{
        .{ .strategy_id = "alpha", .order = .{ .outcome = "filled" } },
    };
    const trades = [_]AnnotatedTrade{
        .{ .strategy_id = "alpha", .trade = .{ .realized_pnl = decText("10") } },
    };
    const attribution = try perStrategyAttribution(std.testing.allocator, &fills, &orders, &trades);
    defer std.testing.allocator.free(attribution);
    try testing.expectEqual(@as(usize, 2), attribution.len);
    try testing.expectEqualStrings("alpha", attribution[0].strategy_id);
    try testing.expectEqualStrings("beta", attribution[1].strategy_id);
    try testing.expectEqual(decText("10"), attribution[0].trades.total_pnl);
    try testing.expectEqual(@as(?i128, null), attribution[0].trades.profit_factor);
    try testing.expectEqual(decText("1"), attribution[0].execution.fees_total);
    try testing.expectEqual(decText("0.5"), attribution[0].execution.slippage_total);
    try testing.expectEqual(@as(usize, 1), attribution[0].execution.outcomes.filled);
    try testing.expectEqual(@as(usize, 0), attribution[1].trades.trade_count);
    try testing.expectEqual(@as(?i128, null), attribution[1].trades.hit_rate);
    try testing.expectEqual(decText("2"), attribution[1].execution.fees_total);
    try testing.expectEqual(@as(usize, 0), attribution[1].execution.outcomes.total);
    const none = try perStrategyAttribution(
        std.testing.allocator,
        &[_]AnnotatedFill{},
        &[_]AnnotatedOrder{},
        &[_]AnnotatedTrade{},
    );
    defer std.testing.allocator.free(none);
    try testing.expectEqual(@as(usize, 0), none.len);
}
