//! Shared vocabulary for the metrics port: ledger row types, result
//! structs, the pinned-scale error set, and the annualization constants.
//!
//! Conventions (mirroring ``src/harness/results/metrics.py``): money and
//! ratios are exact fixed point on the scale-18 i128 kernel; only the
//! final annualization steps leave fixed point for ``f64``; genuinely
//! undefined metrics are ``null`` rather than a sentinel. The documented
//! pinned-scale divergence: Python rounds Decimal arithmetic to 28
//! significant digits, the native kernel rounds half-even at scale 18.

const std = @import("std");
pub const decimal = @import("../decimal.zig");

const dec = decimal.dec;

pub const MetricsError = dec.DecimalError || error{InvalidInput};
pub const AttributionError = MetricsError || error{OutOfMemory};

pub const zero: i128 = 0;
pub const one = dec.one;
pub const nanos_per_day: i64 = 86_400_000_000_000;
pub const nanos_per_year_365: i64 = 365 * nanos_per_day;

pub const EquityPoint = struct {
    /// Epoch nanoseconds; points arrive in ascending order.
    timestamp_ns: i64,
    equity: i128,
};

pub const TradeRow = struct {
    /// Signed realized PnL at the pinned scale.
    realized_pnl: i128,
};

pub const FillRow = struct {
    quantity: i128,
    price: i128,
    fee: i128,
    /// Signed per-fill cost: ``quantity * (fill_price - reference_price)``.
    slippage: i128,
    latency_ns: ?i64 = null,
};

pub const OrderRow = struct {
    /// Exact lowercase outcome: "filled", "rejected", "cancelled",
    /// or anything else (counted as other).
    outcome: []const u8,
};

pub const DrawdownSpan = struct {
    duration_ns: i64,
    recovered: bool,
};

pub const EquityMetrics = struct {
    total_return: ?i128,
    cagr: ?f64,
    max_drawdown: i128,
    drawdown: DrawdownSpan,
};

pub const TradeStats = struct {
    trade_count: usize,
    win_count: usize,
    loss_count: usize,
    hit_rate: ?i128,
    total_pnl: i128,
    gross_profit: i128,
    gross_loss: i128,
    profit_factor: ?i128,
    average_win: ?i128,
    average_loss: ?i128,
    largest_win: ?i128,
    largest_loss: ?i128,
};

pub const LatencyStats = struct {
    count: usize,
    mean_ns: ?i128,
    max_ns: ?i64,
};

pub const OrderOutcomeCounts = struct {
    total: usize,
    filled: usize,
    rejected: usize,
    cancelled: usize,
    other: usize,
};

pub const ExecutionStats = struct {
    fill_count: usize,
    fees_total: i128,
    slippage_total: i128,
    latency: LatencyStats,
    outcomes: OrderOutcomeCounts,
};

pub const StrategyAttribution = struct {
    strategy_id: []const u8,
    trades: TradeStats,
    execution: ExecutionStats,
};

/// Strategy-tagged ledger rows for attribution.
pub const AnnotatedFill = struct { strategy_id: []const u8, fill: FillRow };
pub const AnnotatedOrder = struct { strategy_id: []const u8, order: OrderRow };
pub const AnnotatedTrade = struct { strategy_id: []const u8, trade: TradeRow };

pub fn unscaledToF64(value: i128) f64 {
    return @as(f64, @floatFromInt(value)) / @as(f64, @floatFromInt(dec.one));
}

/// Comptime scale-18 decimal from text (at most 18 fractional digits).
/// A test-fixture helper shared by the metrics modules' colocated tests.
pub fn decText(comptime text: []const u8) i128 {
    var negative = false;
    var body = text;
    if (body[0] == '-') {
        negative = true;
        body = body[1..];
    }
    var integer: i128 = 0;
    var fraction: i128 = 0;
    var fraction_digits: usize = 0;
    var seen_dot = false;
    for (body) |char| {
        if (char == '.') {
            seen_dot = true;
        } else if (!seen_dot) {
            integer = integer * 10 + (char - '0');
        } else {
            if (fraction_digits < 18) {
                fraction = fraction * 10 + (char - '0');
                fraction_digits += 1;
            }
        }
    }
    var scaled = integer * dec.one;
    while (fraction_digits < 18) : (fraction_digits += 1) fraction *= 10;
    scaled += fraction;
    return if (negative) -scaled else scaled;
}

test "decText parses signs, integer, and fractional parts" {
    try std.testing.expectEqual(@as(i128, dec.one), decText("1"));
    try std.testing.expectEqual(@as(i128, -dec.one), decText("-1"));
    try std.testing.expectEqual(@as(i128, 400_000_000_000_000_000), decText("0.4"));
    try std.testing.expectEqual(@as(i128, 4 * dec.one), decText("4.0"));
    try std.testing.expectEqual(@as(i128, 0), decText("0"));
}
