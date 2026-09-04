//! Deterministic top-of-book touch decisions for single-leg orders, ported
//! from ``src/harness/fills/simple_touch.py``.
//!
//! Every decision is an exact i128 fixed-point value at scale 18. Quotes use
//! the ask for buys and the bid for sells; a quote's displayed side quantity
//! bounds a fill, which gives the simulated venue deterministic partial-fill
//! behavior without a random participation model. A trade alone is not a
//! SimpleTouch for a resting limit — print qualification belongs to the
//! trade-through model.

const std = @import("std");
const market = @import("market.zig");

pub const dec = market.dec;
pub const Side = market.Side;
pub const OrderType = market.OrderType;
pub const Error = market.Error;

const MarketEvent = market.MarketEvent;
const MarketOrder = market.MarketOrder;

/// Liquidity flag of a fill decision (Python ``FillDecision.liquidity``).
pub const Liquidity = enum { maker, taker };

/// Venue-side treatment represented by a plugin after a partial fill
/// (Python ``RemainderPolicy``).
pub const RemainderPolicy = enum { cancel, rest };

/// One exact fill decision and the immutable market event that caused it
/// (Python ``FillDecision``).
pub const FillDecision = struct {
    order_id: []const u8,
    quantity: i128,
    price: i128,
    decision_price: i128,
    triggering_market: MarketEvent,
    liquidity: Liquidity = .taker,

    pub fn validate(self: FillDecision) Error!void {
        if (self.order_id.len == 0) return error.InvalidArgument;
        if (self.quantity <= 0) return error.InvalidArgument;
        if (self.price <= 0 or self.decision_price <= 0) return error.InvalidArgument;
        try self.triggering_market.validate();
    }

    /// The execution price under the common fill vocabulary.
    pub fn fillPrice(self: FillDecision) i128 {
        return self.price;
    }
};

/// Keyword-style options shared by every fill model's ``decide``.
pub const DecideOptions = struct {
    remaining_quantity: ?i128 = null,
    available_quantity: ?i128 = null,
    stop_triggered: bool = false,
};

pub fn validatePair(order: MarketOrder, snap: MarketEvent) Error!void {
    try order.validate();
    try snap.validate();
    if (!std.mem.eql(u8, order.symbol, snap.symbol())) return error.InvalidArgument;
}

pub fn resolveRemaining(order: MarketOrder, opts: DecideOptions) Error!i128 {
    const remaining = opts.remaining_quantity orelse order.quantity;
    if (remaining <= 0) return error.InvalidArgument;
    return remaining;
}

pub fn displayedQuantity(side: Side, snap: MarketEvent) i128 {
    return switch (snap) {
        .trade => |trade| trade.quantity,
        .quote => |quote| if (side == .buy) quote.ask_quantity else quote.bid_quantity,
    };
}

/// Fill a single-leg order at the first visible quote or trade touch
/// (Python ``SimpleTouch``).
pub const SimpleTouch = struct {
    pub fn decide(
        self: SimpleTouch,
        order: MarketOrder,
        snap: MarketEvent,
        opts: DecideOptions,
    ) Error!?FillDecision {
        _ = self;
        try validatePair(order, snap);
        const remaining = try resolveRemaining(order, opts);

        var available: i128 = undefined;
        if (opts.available_quantity) |explicit| {
            if (explicit <= 0) return null;
            available = explicit;
        } else {
            available = displayedQuantity(order.side, snap);
        }

        if (order.order_type == .stop or order.order_type == .stop_limit) {
            if (!opts.stop_triggered and !try isTriggered(order, snap)) return null;
        }

        const touch = try touchPrice(order, snap);
        if (!touch.touched) return null;

        const quantity = @min(remaining, available);
        if (quantity <= 0) return null;
        return .{
            .order_id = order.order_id,
            .quantity = quantity,
            .price = touch.price,
            .decision_price = touch.price,
            .triggering_market = snap,
        };
    }

    /// Return whether a stop order is armed by this market snapshot.
    pub fn isTriggered(order: MarketOrder, snap: MarketEvent) Error!bool {
        if (order.order_type != .stop and order.order_type != .stop_limit) return true;
        const stop_price = order.stop_price orelse return error.InvalidArgument;
        return switch (snap) {
            .trade => |trade| if (order.side == .buy)
                trade.price >= stop_price
            else
                trade.price <= stop_price,
            .quote => |quote| if (order.side == .buy)
                quote.ask_price >= stop_price
            else
                quote.bid_price <= stop_price,
        };
    }
};

const Touch = struct { price: i128, touched: bool };

fn touchPrice(order: MarketOrder, snap: MarketEvent) Error!Touch {
    switch (snap) {
        .trade => |trade| {
            if (order.order_type == .market or order.order_type == .stop) {
                return .{ .price = trade.price, .touched = true };
            }
            return .{ .price = trade.price, .touched = false };
        },
        .quote => |quote| {
            if (order.side == .buy) {
                if (order.order_type == .market or order.order_type == .stop) {
                    return .{ .price = quote.ask_price, .touched = true };
                }
                const limit = order.limit_price orelse return error.InvalidArgument;
                return .{ .price = quote.ask_price, .touched = quote.ask_price <= limit };
            }
            if (order.order_type == .market or order.order_type == .stop) {
                return .{ .price = quote.bid_price, .touched = true };
            }
            const limit = order.limit_price orelse return error.InvalidArgument;
            return .{ .price = quote.bid_price, .touched = quote.bid_price >= limit };
        },
    }
}

// ---------------------------------------------------------------------------
// Shared golden-vector fixtures. The latency-size and trade-through tests
// reuse the same STAMP, quotes, trades, and orders as the simple-touch tests
// (STAMP is 2024-01-19T14:30:00Z, mirroring the Python test modules).
// ---------------------------------------------------------------------------

pub const testing = std.testing;

pub const stamp_ns: i128 = 1_705_674_600_000_000_000;
pub const hundred = dec.fromInt(100);
pub const one0one = dec.fromInt(101);
pub const ten = dec.fromInt(10);
pub const three_hundred = dec.fromInt(300);

pub fn makeQuote(
    event_id: []const u8,
    bid: i128,
    ask: i128,
    bid_quantity: i128,
    ask_quantity: i128,
) MarketEvent {
    return .{ .quote = .{
        .event_id = event_id,
        .timestamp_ns = stamp_ns,
        .sequence = 0,
        .symbol = "AAPL",
        .bid_price = bid,
        .bid_quantity = bid_quantity,
        .ask_price = ask,
        .ask_quantity = ask_quantity,
    } };
}

pub fn defaultQuote(event_id: []const u8) MarketEvent {
    return makeQuote(event_id, hundred, one0one, ten, ten);
}

pub fn largeQuote(event_id: []const u8, bid: i128, ask: i128) MarketEvent {
    return makeQuote(event_id, bid, ask, hundred, hundred);
}

pub fn makeTrade(event_id: []const u8, price: i128, quantity: i128) MarketEvent {
    return .{ .trade = .{
        .event_id = event_id,
        .timestamp_ns = stamp_ns,
        .sequence = 0,
        .symbol = "AAPL",
        .price = price,
        .quantity = quantity,
    } };
}

pub fn makeOrder(
    order_id: []const u8,
    side: Side,
    order_type: OrderType,
    quantity: i128,
    limit_price: ?i128,
    stop_price: ?i128,
) MarketOrder {
    return .{
        .order_id = order_id,
        .symbol = "AAPL",
        .side = side,
        .quantity = quantity,
        .order_type = order_type,
        .limit_price = limit_price,
        .stop_price = stop_price,
        .timestamp_ns = stamp_ns,
    };
}

pub fn expectDecision(
    decision: ?FillDecision,
    quantity: i128,
    price: i128,
    liquidity: Liquidity,
) !void {
    try testing.expect(decision != null);
    try testing.expectEqual(quantity, decision.?.quantity);
    try testing.expectEqual(price, decision.?.price);
    try testing.expectEqual(liquidity, decision.?.liquidity);
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/fills/test_simple_touch.py.
// ---------------------------------------------------------------------------

test "simple touch: market orders fill at the correct side" {
    const plugin = SimpleTouch{};
    const buy = try plugin.decide(makeOrder("market-buy", .buy, .market, ten, null, null), defaultQuote("q"), .{});
    try expectDecision(buy, ten, one0one, .taker);
    const sell = try plugin.decide(makeOrder("market-sell", .sell, .market, ten, null, null), defaultQuote("q"), .{});
    try expectDecision(sell, ten, hundred, .taker);
}

test "simple touch: limits cross at the touch and rest when not crossing" {
    const plugin = SimpleTouch{};
    const crossing = try plugin.decide(
        makeOrder("cross-buy", .buy, .limit, ten, one0one, null),
        largeQuote("crossing", hundred, one0one),
        .{},
    );
    try expectDecision(crossing, ten, one0one, .taker);
    const resting = try plugin.decide(
        makeOrder("rest-buy", .buy, .limit, ten, one0one, null),
        largeQuote("resting", dec.fromInt(99), dec.fromInt(102)),
        .{},
    );
    try testing.expect(resting == null);

    const sell_crossing = try plugin.decide(
        makeOrder("cross-sell", .sell, .limit, ten, hundred, null),
        largeQuote("crossing-sell", hundred, one0one),
        .{},
    );
    try expectDecision(sell_crossing, ten, hundred, .taker);
    const sell_resting = try plugin.decide(
        makeOrder("rest-sell", .sell, .limit, ten, hundred, null),
        largeQuote("resting-sell", dec.fromInt(99), dec.fromInt(102)),
        .{},
    );
    try testing.expect(sell_resting == null);
}

test "simple touch: partial quantity is bounded by displayed touch quantity" {
    const plugin = SimpleTouch{};
    const decision = try plugin.decide(
        makeOrder("partial", .buy, .market, ten, null, null),
        makeQuote("small-ask", hundred, one0one, ten, dec.fromInt(3)),
        .{},
    );
    try expectDecision(decision, dec.fromInt(3), one0one, .taker);
}

test "simple touch: stop orders arm only on the correct touch" {
    const plugin = SimpleTouch{};
    const buy_stop = makeOrder("stop-buy", .buy, .stop, ten, null, one0one);
    try testing.expect(try SimpleTouch.isTriggered(buy_stop, defaultQuote("buy-trigger")));
    try testing.expect(!try SimpleTouch.isTriggered(buy_stop, makeQuote("buy-rest", hundred, hundred, ten, ten)));
    try testing.expect((try plugin.decide(buy_stop, makeQuote("buy-rest", hundred, hundred, ten, ten), .{})) == null);
    try testing.expect((try plugin.decide(buy_stop, defaultQuote("buy-trigger"), .{})) != null);

    const sell_stop = makeOrder("stop-sell", .sell, .stop, ten, null, hundred);
    try testing.expect(try SimpleTouch.isTriggered(sell_stop, makeQuote("sell-trigger", hundred, one0one, ten, ten)));
    try testing.expect(!try SimpleTouch.isTriggered(sell_stop, makeQuote("sell-rest", one0one, dec.fromInt(102), ten, ten)));
    try testing.expect((try plugin.decide(sell_stop, makeQuote("sell-rest", one0one, dec.fromInt(102), ten, ten), .{})) == null);
    try testing.expect((try plugin.decide(sell_stop, makeQuote("sell-trigger", hundred, one0one, ten, ten), .{})) != null);
}

test "simple touch: stop limit requires both trigger and limit touch" {
    const plugin = SimpleTouch{};
    const stop_order = makeOrder("stop-limit", .buy, .stop_limit, ten, dec.fromInt(102), one0one);
    const decision = try plugin.decide(stop_order, defaultQuote("trigger"), .{});
    try testing.expect(decision != null);
    try testing.expectEqualStrings("trigger", decision.?.triggering_market.eventId());

    const no_limit_touch = makeOrder("stop-limit-rest", .buy, .stop_limit, ten, hundred + 500_000_000_000_000_000, one0one);
    try testing.expect((try plugin.decide(no_limit_touch, defaultQuote("armed"), .{})) == null);
}

test "simple touch: trade snapshots are supported without float conversion" {
    const plugin = SimpleTouch{};
    const traded = makeTrade("trade", hundred + 250_000_000_000_000_000, dec.fromInt(2));
    const decision = try plugin.decide(
        makeOrder("trade-market", .sell, .market, dec.fromInt(3), null, null),
        traded,
        .{},
    );
    try expectDecision(decision, dec.fromInt(2), hundred + 250_000_000_000_000_000, .taker);
}

test "simple touch: a trade does not qualify a resting limit touch" {
    const plugin = SimpleTouch{};
    const traded = makeTrade("trade-limit", dec.fromInt(99), ten);
    const decision = try plugin.decide(
        makeOrder("limit-trade", .buy, .limit, dec.fromInt(2), hundred, null),
        traded,
        .{},
    );
    try testing.expect(decision == null);
}
