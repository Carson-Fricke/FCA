//! Deterministic latency and displayed-size constrained fill decisions,
//! ported from ``src/harness/fills/latency_size.py``.
//!
//! The model deliberately keeps response latency as plugin metadata
//! (integer microseconds): the simulated venue owns response scheduling,
//! while this plugin owns the execution assumptions that produced a
//! ``FillDecision``. Marketable orders use the visible quote side; a
//! non-marketable limit order is pessimistically resting by default and
//! needs a qualifying trade print. Setting ``require_trade_through = false``
//! enables the explicitly optimistic alternative of filling a limit inside
//! the current spread at its limit price.

const std = @import("std");
const market = @import("market.zig");
const st = @import("simple_touch.zig");

pub const Error = market.Error;
pub const dec = market.dec;
pub const Side = market.Side;
pub const OrderType = market.OrderType;

const MarketEvent = market.MarketEvent;
const MarketOrder = market.MarketOrder;
const FillDecision = st.FillDecision;
const DecideOptions = st.DecideOptions;
const RemainderPolicy = st.RemainderPolicy;
const Liquidity = st.Liquidity;
const SimpleTouch = st.SimpleTouch;

/// Independent configuration for ``LatencySize`` (Python
/// ``LatencySizeConfig``). ``response_latency_us`` is metadata consumed by an
/// execution adapter; it does not alter the decision snapshot.
pub const LatencySizeConfig = struct {
    response_latency_us: i64 = 0,
    remainder_policy: RemainderPolicy = .rest,
    require_trade_through: bool = true,
};

/// Stable execution metadata accompanying a latency-size plugin (Python
/// ``LatencyMetadata``).
pub const LatencyMetadata = struct {
    response_latency_us: i64,
    displayed_size_constrained: bool = true,
    remainder_policy: RemainderPolicy,
    require_trade_through: bool,
};

/// Fill single-leg orders with deterministic latency and size rules (Python
/// ``LatencySize``). The model is intentionally stateful only for the cancel
/// remainder policy: each order identifier is independently marked when its
/// first decision is partial. The caller still supplies the authoritative
/// remaining quantity on every invocation.
pub const LatencySize = struct {
    gpa: std.mem.Allocator,
    config: LatencySizeConfig,
    cancelled: std.ArrayList([]const u8) = .empty,

    pub fn init(gpa: std.mem.Allocator, config: LatencySizeConfig) LatencySize {
        return .{ .gpa = gpa, .config = config };
    }

    pub fn deinit(self: *LatencySize) void {
        self.cancelled.deinit(self.gpa);
        self.cancelled = .empty;
    }

    pub fn metadata(self: *const LatencySize) LatencyMetadata {
        return .{
            .response_latency_us = self.config.response_latency_us,
            .remainder_policy = self.config.remainder_policy,
            .require_trade_through = self.config.require_trade_through,
        };
    }

    /// Return whether this model has canceled an order's remainder.
    pub fn isCancelled(self: *const LatencySize, order_id: []const u8) bool {
        for (self.cancelled.items) |cancelled| {
            if (std.mem.eql(u8, cancelled, order_id)) return true;
        }
        return false;
    }

    pub fn decide(
        self: *LatencySize,
        order: MarketOrder,
        snap: MarketEvent,
        opts: DecideOptions,
    ) Error!?FillDecision {
        try st.validatePair(order, snap);
        if (self.isCancelled(order.order_id)) return null;
        const remaining = try st.resolveRemaining(order, opts);

        var size: i128 = st.displayedQuantity(order.side, snap);
        if (opts.available_quantity) |explicit| {
            if (explicit <= 0) return null;
            size = explicit;
        }

        if (order.order_type == .stop or order.order_type == .stop_limit) {
            if (!opts.stop_triggered and !try SimpleTouch.isTriggered(order, snap)) return null;
        }

        const outcome = (try self.execution(order, snap)) orelse return null;
        const quantity = @min(remaining, size);
        if (quantity <= 0) return null;
        const decision = FillDecision{
            .order_id = order.order_id,
            .quantity = quantity,
            .price = outcome.price,
            .decision_price = outcome.price,
            .triggering_market = snap,
            .liquidity = outcome.liquidity,
        };
        if (quantity < remaining and self.config.remainder_policy == .cancel) {
            try self.cancelled.append(self.gpa, order.order_id);
        }
        return decision;
    }

    const Execution = struct { price: i128, liquidity: Liquidity };

    fn requiredLimit(order: MarketOrder) Error!i128 {
        return order.limit_price orelse error.InvalidArgument;
    }

    fn qualifyingPrint(order: MarketOrder, price: i128) Error!bool {
        const limit = try requiredLimit(order);
        return if (order.side == .buy) price <= limit else price >= limit;
    }

    fn execution(self: *const LatencySize, order: MarketOrder, snap: MarketEvent) Error!?Execution {
        switch (snap) {
            .trade => |trade| {
                if (order.order_type == .market or order.order_type == .stop) {
                    return .{ .price = trade.price, .liquidity = .taker };
                }
                if (try qualifyingPrint(order, trade.price)) {
                    return .{ .price = trade.price, .liquidity = .maker };
                }
                return null;
            },
            .quote => |quote| {
                if (order.side == .buy) {
                    if (order.order_type == .market or order.order_type == .stop) {
                        return .{ .price = quote.ask_price, .liquidity = .taker };
                    }
                    const limit = try requiredLimit(order);
                    if (quote.ask_price <= limit) {
                        return .{ .price = quote.ask_price, .liquidity = .taker };
                    }
                    if (!self.config.require_trade_through and
                        quote.bid_price < limit and limit < quote.ask_price)
                    {
                        return .{ .price = limit, .liquidity = .maker };
                    }
                    return null;
                }
                if (order.order_type == .market or order.order_type == .stop) {
                    return .{ .price = quote.bid_price, .liquidity = .taker };
                }
                const limit = try requiredLimit(order);
                if (quote.bid_price >= limit) {
                    return .{ .price = quote.bid_price, .liquidity = .taker };
                }
                if (!self.config.require_trade_through and
                    quote.bid_price < limit and limit < quote.ask_price)
                {
                    return .{ .price = limit, .liquidity = .maker };
                }
                return null;
            },
        }
    }
};

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/fills/test_latency_size.py
// (the YAML-loading scenarios stay Python-side; the native boundary is the
// configuration struct).
// ---------------------------------------------------------------------------

const testing = st.testing;
const makeQuote = st.makeQuote;
const makeTrade = st.makeTrade;
const makeOrder = st.makeOrder;
const defaultQuote = st.defaultQuote;
const largeQuote = st.largeQuote;
const expectDecision = st.expectDecision;
const hundred = st.hundred;
const one0one = st.one0one;
const ten = st.ten;
const three_hundred = st.three_hundred;

test "latency size: displayed size bounds fills and applies remainder policy" {
    var cancel_model = LatencySize.init(testing.allocator, .{ .remainder_policy = .cancel });
    defer cancel_model.deinit();
    var rest_model = LatencySize.init(testing.allocator, .{ .remainder_policy = .rest });
    defer rest_model.deinit();
    const buy = makeOrder("size-cancel", .buy, .market, three_hundred, null, null);

    const first = try cancel_model.decide(buy, largeQuote("q-1", hundred, one0one), .{});
    try expectDecision(first, hundred, one0one, .taker);
    try testing.expect(cancel_model.isCancelled(buy.order_id));

    const cancelled_second = try cancel_model.decide(buy, largeQuote("q-2", hundred, one0one), .{ .remaining_quantity = dec.fromInt(200) });
    try testing.expect(cancelled_second == null);

    const rest_first = try rest_model.decide(buy, largeQuote("q-1", hundred, one0one), .{});
    try expectDecision(rest_first, hundred, one0one, .taker);
    try testing.expect(!rest_model.isCancelled(buy.order_id));
    const rest_second = try rest_model.decide(buy, largeQuote("q-2", hundred, one0one), .{ .remaining_quantity = dec.fromInt(200) });
    try expectDecision(rest_second, hundred, one0one, .taker);
}

test "latency size: partial fills across quote events are snapshot deterministic" {
    var model = LatencySize.init(testing.allocator, .{});
    defer model.deinit();
    const buy = makeOrder("snapshots", .buy, .market, three_hundred, null, null);
    const first_quote = largeQuote("q-1", hundred, one0one);
    const second_quote = largeQuote("q-2", hundred, hundred);

    const first = try model.decide(buy, first_quote, .{});
    const second = try model.decide(buy, second_quote, .{ .remaining_quantity = dec.fromInt(200) });
    try expectDecision(first, hundred, one0one, .taker);
    try expectDecision(second, hundred, hundred, .taker);
    try testing.expectEqualStrings("q-1", first.?.triggering_market.eventId());
    try testing.expectEqualStrings("q-2", second.?.triggering_market.eventId());

    const repeat = try model.decide(buy, first_quote, .{ .remaining_quantity = hundred });
    try testing.expect(repeat != null);
    try testing.expectEqual(first.?.quantity, repeat.?.quantity);
    try testing.expectEqual(first.?.price, repeat.?.price);
}

test "latency size: inside-spread limit requires a qualifying print by default" {
    var model = LatencySize.init(testing.allocator, .{});
    defer model.deinit();
    const buy = makeOrder("inside", .buy, .limit, three_hundred, hundred + 500_000_000_000_000_000, null);

    try testing.expect((try model.decide(buy, largeQuote("q-1", hundred, one0one), .{})) == null);
    try testing.expect((try model.decide(buy, makeTrade("t-1", hundred + 750_000_000_000_000_000, hundred), .{})) == null);

    const decision = try model.decide(buy, makeTrade("t-2", hundred + 500_000_000_000_000_000, hundred), .{});
    try expectDecision(decision, hundred, hundred + 500_000_000_000_000_000, .maker);
}

test "latency size: configuration changes latency and inside-spread outcome" {
    var pessimistic = LatencySize.init(testing.allocator, .{ .response_latency_us = 25, .require_trade_through = true });
    defer pessimistic.deinit();
    var optimistic = LatencySize.init(testing.allocator, .{ .response_latency_us = 50, .require_trade_through = false });
    defer optimistic.deinit();
    const buy = makeOrder("parameters", .buy, .limit, three_hundred, hundred + 500_000_000_000_000_000, null);
    const inside = largeQuote("q-1", hundred, one0one);

    try testing.expectEqual(@as(i64, 25), pessimistic.metadata().response_latency_us);
    try testing.expect((try pessimistic.decide(buy, inside, .{})) == null);
    const decision = try optimistic.decide(buy, inside, .{});
    try expectDecision(decision, hundred, hundred + 500_000_000_000_000_000, .maker);
    try testing.expectEqual(@as(i64, 50), optimistic.metadata().response_latency_us);
}
