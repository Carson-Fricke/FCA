//! Strict deterministic trade-print fill decisions, ported from
//! ``src/harness/fills/trade_through.py``.
//!
//! Quote events never fill this model, including marketable limits. A buy
//! limit requires a print strictly below its limit by default; a sell limit
//! requires a print strictly above it. ``minimum_trade_through`` adds an
//! exact price distance. Setting ``strict = false`` makes a print at the
//! limit qualifying while retaining the print-only rule.

const std = @import("std");
const market = @import("market.zig");
const st = @import("simple_touch.zig");

pub const Error = market.Error;
pub const dec = market.dec;

const MarketEvent = market.MarketEvent;
const MarketOrder = market.MarketOrder;
const FillDecision = st.FillDecision;
const DecideOptions = st.DecideOptions;
const RemainderPolicy = st.RemainderPolicy;
const Liquidity = st.Liquidity;
const SimpleTouch = st.SimpleTouch;

/// Independent configuration for the strict print-only model (Python
/// ``TradeThroughConfig``).
pub const TradeThroughConfig = struct {
    response_latency_us: i64 = 0,
    minimum_trade_through: i128 = 0,
    strict: bool = true,
    remainder_policy: RemainderPolicy = .rest,
};

/// Stable assumptions for a print-only trade-through plugin (Python
/// ``TradeThroughMetadata``).
pub const TradeThroughMetadata = struct {
    response_latency_us: i64,
    displayed_size_constrained: bool = true,
    minimum_trade_through: i128,
    strict: bool,
    remainder_policy: RemainderPolicy,
};

/// Require qualifying trade prints for every executable order (Python
/// ``TradeThrough``).
pub const TradeThrough = struct {
    gpa: std.mem.Allocator,
    config: TradeThroughConfig,
    cancelled: std.ArrayList([]const u8) = .empty,

    pub fn init(gpa: std.mem.Allocator, config: TradeThroughConfig) Error!TradeThrough {
        if (config.minimum_trade_through < 0) return error.InvalidArgument;
        return .{ .gpa = gpa, .config = config };
    }

    pub fn deinit(self: *TradeThrough) void {
        self.cancelled.deinit(self.gpa);
        self.cancelled = .empty;
    }

    pub fn metadata(self: *const TradeThrough) TradeThroughMetadata {
        return .{
            .response_latency_us = self.config.response_latency_us,
            .minimum_trade_through = self.config.minimum_trade_through,
            .strict = self.config.strict,
            .remainder_policy = self.config.remainder_policy,
        };
    }

    /// Return whether this model has canceled an order's remainder.
    pub fn isCancelled(self: *const TradeThrough, order_id: []const u8) bool {
        for (self.cancelled.items) |cancelled| {
            if (std.mem.eql(u8, cancelled, order_id)) return true;
        }
        return false;
    }

    pub fn decide(
        self: *TradeThrough,
        order: MarketOrder,
        snap: MarketEvent,
        opts: DecideOptions,
    ) Error!?FillDecision {
        try st.validatePair(order, snap);
        if (self.isCancelled(order.order_id)) return null;
        const remaining = try st.resolveRemaining(order, opts);

        const trade = switch (snap) {
            .trade => |value| value,
            .quote => return null,
        };
        var available: i128 = trade.quantity;
        if (opts.available_quantity) |explicit| {
            if (explicit <= 0) return null;
            available = explicit;
        }

        if (order.order_type == .stop or order.order_type == .stop_limit) {
            if (!opts.stop_triggered and !try SimpleTouch.isTriggered(order, snap)) return null;
        }
        if (order.order_type == .limit or order.order_type == .stop_limit) {
            if (!try qualifies(self.config, order, trade.price)) return null;
        }

        const quantity = @min(remaining, available);
        if (quantity <= 0) return null;
        const liquidity: Liquidity = if (order.order_type == .limit or order.order_type == .stop_limit)
            .maker
        else
            .taker;
        const decision = FillDecision{
            .order_id = order.order_id,
            .quantity = quantity,
            .price = trade.price,
            .decision_price = trade.price,
            .triggering_market = snap,
            .liquidity = liquidity,
        };
        if (quantity < remaining and self.config.remainder_policy == .cancel) {
            try self.cancelled.append(self.gpa, order.order_id);
        }
        return decision;
    }

    fn qualifies(config: TradeThroughConfig, order: MarketOrder, price: i128) Error!bool {
        const limit = order.limit_price orelse return error.InvalidArgument;
        if (order.side == .buy) {
            const threshold = try dec.sub(limit, config.minimum_trade_through);
            return if (config.strict) price < threshold else price <= threshold;
        }
        const threshold = try dec.add(limit, config.minimum_trade_through);
        return if (config.strict) price > threshold else price >= threshold;
    }
};

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/fills/test_trade_through.py
// (the YAML-loading scenarios stay Python-side; the native boundary is the
// configuration struct).
// ---------------------------------------------------------------------------

const testing = st.testing;
const makeTrade = st.makeTrade;
const makeOrder = st.makeOrder;
const largeQuote = st.largeQuote;
const expectDecision = st.expectDecision;
const hundred = st.hundred;
const three_hundred = st.three_hundred;

test "trade through: strict model requires a print beyond the limit" {
    var model = try TradeThrough.init(testing.allocator, .{});
    defer model.deinit();
    const buy = makeOrder("strict", .buy, .limit, three_hundred, hundred + 500_000_000_000_000_000, null);

    try testing.expect((try model.decide(buy, largeQuote("quote", hundred, hundred + 500_000_000_000_000_000), .{})) == null);
    try testing.expect((try model.decide(buy, makeTrade("trade", hundred + 500_000_000_000_000_000, hundred), .{})) == null);

    const decision = try model.decide(buy, makeTrade("through", hundred + 490_000_000_000_000_000, hundred), .{});
    try expectDecision(decision, hundred, hundred + 490_000_000_000_000_000, .maker);
}

test "trade through: print qualification is directional" {
    var model = try TradeThrough.init(testing.allocator, .{});
    defer model.deinit();
    const buy = makeOrder("direction-buy", .buy, .limit, three_hundred, hundred + 500_000_000_000_000_000, null);
    try testing.expect((try model.decide(buy, makeTrade("at-limit", hundred + 500_000_000_000_000_000, hundred), .{})) == null);
    const buy_through = try model.decide(buy, makeTrade("through", hundred + 490_000_000_000_000_000, hundred), .{});
    try expectDecision(buy_through, hundred, hundred + 490_000_000_000_000_000, .maker);

    const sell = makeOrder("direction-sell", .sell, .limit, three_hundred, hundred + 500_000_000_000_000_000, null);
    try testing.expect((try model.decide(sell, makeTrade("at-limit", hundred + 500_000_000_000_000_000, hundred), .{})) == null);
    const sell_through = try model.decide(sell, makeTrade("through", hundred + 510_000_000_000_000_000, hundred), .{});
    try expectDecision(sell_through, hundred, hundred + 510_000_000_000_000_000, .maker);
}

test "trade through: print size and remainder policy" {
    var cancel_model = try TradeThrough.init(testing.allocator, .{ .remainder_policy = .cancel });
    defer cancel_model.deinit();
    var rest_model = try TradeThrough.init(testing.allocator, .{ .remainder_policy = .rest });
    defer rest_model.deinit();
    const cancel_order = makeOrder("cancel", .buy, .limit, three_hundred, hundred + 500_000_000_000_000_000, null);
    const rest_order = makeOrder("rest", .buy, .limit, three_hundred, hundred + 500_000_000_000_000_000, null);
    const print_event = makeTrade("print", hundred + 490_000_000_000_000_000, hundred);

    const cancel_decision = try cancel_model.decide(cancel_order, print_event, .{});
    const rest_decision = try rest_model.decide(rest_order, print_event, .{});
    try testing.expect(cancel_decision != null and rest_decision != null);
    try testing.expectEqual(cancel_decision.?.quantity, rest_decision.?.quantity);
    try testing.expectEqual(hundred, rest_decision.?.quantity);
    try testing.expect(cancel_model.isCancelled(cancel_order.order_id));
    try testing.expect(!rest_model.isCancelled(rest_order.order_id));

    const second = try rest_model.decide(rest_order, makeTrade("trade-2", hundred + 480_000_000_000_000_000, hundred), .{ .remaining_quantity = dec.fromInt(200) });
    try testing.expect(second != null);
}

test "trade through: parameters change threshold and metadata" {
    var default_model = try TradeThrough.init(testing.allocator, .{});
    defer default_model.deinit();
    var configured = try TradeThrough.init(testing.allocator, .{
        .response_latency_us = 40,
        .minimum_trade_through = 100_000_000_000_000_000,
        .strict = false,
    });
    defer configured.deinit();
    const buy = makeOrder("parameters", .buy, .limit, three_hundred, hundred + 500_000_000_000_000_000, null);

    try testing.expect((try default_model.decide(buy, makeTrade("trade", hundred + 500_000_000_000_000_000, hundred), .{})) == null);
    const decision = try configured.decide(buy, makeTrade("configured", hundred + 400_000_000_000_000_000, hundred), .{});
    try expectDecision(decision, hundred, hundred + 400_000_000_000_000_000, .maker);
    try testing.expectEqual(@as(i64, 40), configured.metadata().response_latency_us);
    try testing.expectEqual(@as(i128, 100_000_000_000_000_000), configured.metadata().minimum_trade_through);
}

test "trade through: rejects an invalid minimum trade-through" {
    try testing.expectError(
        error.InvalidArgument,
        TradeThrough.init(testing.allocator, .{ .minimum_trade_through = -10_000_000_000_000_000 }),
    );
}
