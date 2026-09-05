//! Realized-trade aggregates: counts, hit rate, gross profit/loss,
//! profit factor, averages, and extremes. Tests are colocated and pin
//! the hand-computed vectors from ``tests/harness/results/test_metrics.py``.

const std = @import("std");
const common = @import("common.zig");

const dec = common.decimal.dec;
const TradeRow = common.TradeRow;
const TradeStats = common.TradeStats;
const MetricsError = common.MetricsError;

/// Aggregate realized trade PnL values. Wins are strictly positive and
/// losses strictly negative; zero-PnL trades count in ``trade_count``
/// but in neither bucket. ``gross_loss`` is the non-negative loss
/// magnitude and ``profit_factor`` is ``null`` without losses.
pub fn tradeStats(trades: []const TradeRow) MetricsError!TradeStats {
    var total = common.zero;
    var gross_profit = common.zero;
    var gross_loss = common.zero;
    var wins: usize = 0;
    var losses: usize = 0;
    var sum_win = common.zero;
    var sum_loss = common.zero;
    var largest_win: ?i128 = null;
    var largest_loss: ?i128 = null;
    for (trades) |row| {
        const pnl = row.realized_pnl;
        total = try dec.add(total, pnl);
        if (pnl > common.zero) {
            wins += 1;
            sum_win = try dec.add(sum_win, pnl);
            gross_profit = try dec.add(gross_profit, pnl);
            largest_win = if (largest_win) |current| @max(current, pnl) else pnl;
        } else if (pnl < common.zero) {
            losses += 1;
            sum_loss = try dec.add(sum_loss, pnl);
            gross_loss = try dec.sub(gross_loss, pnl);
            largest_loss = if (largest_loss) |current| @min(current, pnl) else pnl;
        }
    }
    const trade_count = trades.len;
    return .{
        .trade_count = trade_count,
        .win_count = wins,
        .loss_count = losses,
        .hit_rate = if (trade_count > 0)
            try dec.div(@as(i128, wins) * common.one, @as(i128, trade_count) * common.one)
        else
            null,
        .total_pnl = total,
        .gross_profit = gross_profit,
        .gross_loss = gross_loss,
        .profit_factor = if (gross_loss > common.zero) try dec.div(gross_profit, gross_loss) else null,
        .average_win = if (wins > 0) try dec.div(sum_win, @as(i128, wins) * common.one) else null,
        .average_loss = if (losses > 0) try dec.div(sum_loss, @as(i128, losses) * common.one) else null,
        .largest_win = largest_win,
        .largest_loss = largest_loss,
    };
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/results/test_metrics.py.
// ---------------------------------------------------------------------------

const decText = common.decText;
const testing = std.testing;

fn trade(pnl: i128) TradeRow {
    return .{ .realized_pnl = pnl };
}

test "trade stats pin the five-trade hand-computed vector" {
    const trades = [_]TradeRow{
        trade(decText("100")),
        trade(decText("-50")),
        trade(decText("30")),
        trade(decText("-20")),
        trade(decText("0")),
    };
    const stats = try tradeStats(&trades);
    try testing.expectEqual(@as(usize, 5), stats.trade_count);
    try testing.expectEqual(@as(usize, 2), stats.win_count);
    try testing.expectEqual(@as(usize, 2), stats.loss_count);
    try testing.expectEqual(decText("0.4"), stats.hit_rate.?);
    try testing.expectEqual(decText("60"), stats.total_pnl);
    try testing.expectEqual(decText("130"), stats.gross_profit);
    try testing.expectEqual(decText("70"), stats.gross_loss);
    // 130/70 pinned at scale 18: the 19th fractional digit (8) rounds
    // the repeating 857142 quotient up to ...143 (Python reports 28
    // significant digits; see the module-family docstring).
    try testing.expectEqual(@as(i128, 1_857_142_857_142_857_143), stats.profit_factor.?);
    try testing.expectEqual(decText("65"), stats.average_win.?);
    try testing.expectEqual(decText("-35"), stats.average_loss.?);
    try testing.expectEqual(decText("100"), stats.largest_win.?);
    try testing.expectEqual(decText("-50"), stats.largest_loss.?);
}

test "trade stats no-loss, all-loss, and empty ledgers" {
    const no_loss = [_]TradeRow{ trade(decText("10")), trade(decText("5")) };
    var stats = try tradeStats(&no_loss);
    try testing.expectEqual(decText("0"), stats.gross_loss);
    try testing.expectEqual(@as(?i128, null), stats.profit_factor);
    try testing.expectEqual(decText("1"), stats.hit_rate.?);
    const all_loss = [_]TradeRow{ trade(decText("-5")), trade(decText("-5")) };
    stats = try tradeStats(&all_loss);
    try testing.expectEqual(decText("0"), stats.hit_rate.?);
    try testing.expectEqual(decText("0"), stats.profit_factor.?);
    try testing.expectEqual(decText("-10"), stats.total_pnl);
    stats = try tradeStats(&[_]TradeRow{});
    try testing.expectEqual(@as(usize, 0), stats.trade_count);
    try testing.expectEqual(@as(?i128, null), stats.hit_rate);
    try testing.expectEqual(@as(?i128, null), stats.profit_factor);
    try testing.expectEqual(decText("0"), stats.total_pnl);
}
