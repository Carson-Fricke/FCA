//! Market-data and order domain records for the native replay/venue modules.
//!
//! Ports the deterministic market records from ``src/harness/core/types``
//! (``QuoteEvent``/``TradeEvent``) and the price-bearing single-leg order
//! (``SingleLegOrder`` with ``limit_price``/``stop_price``) used by
//! ``src/harness/fills`` and ``src/harness/venues/sim.py``. Envelope fields
//! use the native conventions: i128 epoch-nanosecond timestamps (see
//! ``clock.zig``), u64 sequences (Python sequences are validated
//! non-negative), and i128 unscaled decimals at scale 18 (``decimal.zig``).
//!
//! Validation mirrors the Python ``__post_init__`` contracts: identifiers and
//! symbols are non-empty, symbols must already be canonical (canonicalization
//! stays a Python-side adapter concern, exactly like the codecs row boundary
//! in ``types.zig``), prices and quantities are strictly positive, and order
//! types carry exactly the limit/stop prices their type allows.

const std = @import("std");
const bus = @import("bus.zig");
const oms = @import("oms.zig");
const types = @import("types.zig");

pub const dec = oms.dec;
pub const Side = oms.Side;
pub const OrderType = oms.OrderType;
pub const TimeInForce = oms.TimeInForce;

pub const Error = error{
    OutOfMemory,
    InvalidArgument,
    InvalidSymbol,
    InvalidDecimal,
    OutOfRange,
};

fn requireCanonicalSymbol(symbol: []const u8) Error!void {
    if (symbol.len == 0) return error.InvalidArgument;
    var buffer: [256]u8 = undefined;
    if (symbol.len > buffer.len) return error.InvalidSymbol;
    const canonical = types.canonicalSymbol(symbol, &buffer) catch |err| switch (err) {
        error.BufferTooSmall => return error.InvalidSymbol,
        else => return error.InvalidSymbol,
    };
    if (!std.mem.eql(u8, canonical.value, symbol)) return error.InvalidSymbol;
}

fn requireText(value: []const u8) Error!void {
    if (value.len == 0) return error.InvalidArgument;
}

fn requirePositive(value: i128) Error!void {
    if (value <= 0) return error.InvalidArgument;
}

/// A top-of-book quote (Python ``QuoteEvent``). All four book values are
/// required, finite, and strictly positive.
pub const QuoteEvent = struct {
    event_id: []const u8,
    timestamp_ns: i128,
    sequence: u64,
    symbol: []const u8,
    bid_price: i128,
    bid_quantity: i128,
    ask_price: i128,
    ask_quantity: i128,
    venue: ?[]const u8 = null,

    pub fn init(event: QuoteEvent) Error!QuoteEvent {
        try validate(event);
        return event;
    }

    pub fn validate(event: QuoteEvent) Error!void {
        try requireText(event.event_id);
        try requireCanonicalSymbol(event.symbol);
        try requirePositive(event.bid_price);
        try requirePositive(event.bid_quantity);
        try requirePositive(event.ask_price);
        try requirePositive(event.ask_quantity);
        if (event.venue) |venue| try requireText(venue);
    }
};

/// A market trade (Python ``TradeEvent``) with exact price and quantity.
pub const TradeEvent = struct {
    event_id: []const u8,
    timestamp_ns: i128,
    sequence: u64,
    symbol: []const u8,
    price: i128,
    quantity: i128,
    venue: ?[]const u8 = null,
    trade_id: ?[]const u8 = null,

    pub fn init(event: TradeEvent) Error!TradeEvent {
        try validate(event);
        return event;
    }

    pub fn validate(event: TradeEvent) Error!void {
        try requireText(event.event_id);
        try requireCanonicalSymbol(event.symbol);
        try requirePositive(event.price);
        try requirePositive(event.quantity);
        if (event.venue) |venue| try requireText(venue);
        if (event.trade_id) |trade_id| try requireText(trade_id);
    }
};

/// The two market snapshots fill models and the simulated venue consume,
/// mirroring Python's ``QuoteEvent | TradeEvent`` market snapshot union.
pub const MarketEvent = union(enum) {
    quote: QuoteEvent,
    trade: TradeEvent,

    pub fn eventId(self: MarketEvent) []const u8 {
        return switch (self) {
            .quote => |event| event.event_id,
            .trade => |event| event.event_id,
        };
    }

    pub fn timestampNs(self: MarketEvent) i128 {
        return switch (self) {
            .quote => |event| event.timestamp_ns,
            .trade => |event| event.timestamp_ns,
        };
    }

    pub fn sequence(self: MarketEvent) u64 {
        return switch (self) {
            .quote => |event| event.sequence,
            .trade => |event| event.sequence,
        };
    }

    pub fn symbol(self: MarketEvent) []const u8 {
        return switch (self) {
            .quote => |event| event.symbol,
            .trade => |event| event.symbol,
        };
    }

    pub fn eventType(self: MarketEvent) bus.EventType {
        return switch (self) {
            .quote => .quote,
            .trade => .trade,
        };
    }

    /// Class priority from the shared ordering contract
    /// (``bus.EVENT_CLASS_PRIORITY``, mirrored from
    /// ``harness.core.events.EVENT_CLASS_PRIORITY``).
    pub fn classPriority(self: MarketEvent) u8 {
        return bus.classPriority(self.eventType());
    }

    pub fn validate(self: MarketEvent) Error!void {
        return switch (self) {
            .quote => |event| QuoteEvent.validate(event),
            .trade => |event| TradeEvent.validate(event),
        };
    }
};

fn pricesForType(order_type: OrderType, limit_price: ?i128, stop_price: ?i128) Error!void {
    const valid = switch (order_type) {
        .market => .{ false, false },
        .limit => .{ true, false },
        .stop => .{ false, true },
        .stop_limit => .{ true, true },
    };
    const has_limit = limit_price != null;
    const has_stop = stop_price != null;
    if (has_limit != valid[0] or has_stop != valid[1]) return error.InvalidArgument;
    if (limit_price) |price| try requirePositive(price);
    if (stop_price) |price| try requirePositive(price);
}

/// A single-leg order with its execution prices (the view of Python's
/// ``SingleLegOrder`` consumed by fill models and the simulated venue).
/// The event envelope timestamp/sequence ride along because the venue
/// observes them exactly like any other event.
pub const MarketOrder = struct {
    order_id: []const u8,
    symbol: []const u8,
    side: Side,
    quantity: i128,
    order_type: OrderType = .market,
    time_in_force: TimeInForce = .day,
    limit_price: ?i128 = null,
    stop_price: ?i128 = null,
    timestamp_ns: i128 = 0,
    sequence: u64 = 0,

    pub fn init(order: MarketOrder) Error!MarketOrder {
        try validate(order);
        return order;
    }

    pub fn validate(order: MarketOrder) Error!void {
        try requireText(order.order_id);
        try requireCanonicalSymbol(order.symbol);
        try requirePositive(order.quantity);
        try pricesForType(order.order_type, order.limit_price, order.stop_price);
    }
};

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror the record contracts pinned by the Python
// domain types (positive decimals, canonical symbols, per-type prices).
// ---------------------------------------------------------------------------

const testing = std.testing;

const stamp_ns: i128 = 1_705_674_600_000_000_000;

test "market events validate like the Python records" {
    const quote = try QuoteEvent.init(.{
        .event_id = "q",
        .timestamp_ns = stamp_ns,
        .sequence = 0,
        .symbol = "AAPL",
        .bid_price = dec.fromInt(100),
        .bid_quantity = dec.fromInt(10),
        .ask_price = dec.fromInt(101),
        .ask_quantity = dec.fromInt(10),
    });
    const quote_view = MarketEvent{ .quote = quote };
    try testing.expectEqual(@as(u8, 0), quote_view.classPriority());
    try testing.expectEqualStrings("AAPL", quote_view.symbol());

    const trade = try TradeEvent.init(.{
        .event_id = "t",
        .timestamp_ns = stamp_ns,
        .sequence = 1,
        .symbol = "AAPL",
        .price = dec.fromInt(100) + 500_000_000_000_000_000,
        .quantity = dec.one,
        .trade_id = "t-1",
    });
    const trade_view = MarketEvent{ .trade = trade };
    try testing.expectEqual(@as(u8, 1), trade_view.classPriority());
    try testing.expectEqual(bus.EventType.trade, trade_view.eventType());

    // Non-positive values, empty identifiers, and non-canonical symbols are
    // rejected exactly like the Python record validation.
    try testing.expectError(error.InvalidArgument, QuoteEvent.init(.{
        .event_id = "q",
        .timestamp_ns = stamp_ns,
        .sequence = 0,
        .symbol = "AAPL",
        .bid_price = 0,
        .bid_quantity = dec.one,
        .ask_price = dec.one,
        .ask_quantity = dec.one,
    }));
    try testing.expectError(error.InvalidSymbol, TradeEvent.init(.{
        .event_id = "t",
        .timestamp_ns = stamp_ns,
        .sequence = 0,
        .symbol = "aapl",
        .price = dec.one,
        .quantity = dec.one,
    }));
    try testing.expectError(error.InvalidArgument, TradeEvent.init(.{
        .event_id = "",
        .timestamp_ns = stamp_ns,
        .sequence = 0,
        .symbol = "AAPL",
        .price = dec.one,
        .quantity = dec.one,
    }));
}

test "orders carry exactly the prices their type allows" {
    const market_order = try MarketOrder.init(.{
        .order_id = "m",
        .symbol = "AAPL",
        .side = .buy,
        .quantity = dec.fromInt(10),
    });
    try testing.expectEqual(OrderType.market, market_order.order_type);

    const limit_order = try MarketOrder.init(.{
        .order_id = "l",
        .symbol = "AAPL",
        .side = .buy,
        .quantity = dec.fromInt(10),
        .order_type = .limit,
        .limit_price = dec.fromInt(101),
    });
    try testing.expect(limit_order.limit_price != null);

    try testing.expectError(error.InvalidArgument, MarketOrder.init(.{
        .order_id = "bad-limit",
        .symbol = "AAPL",
        .side = .buy,
        .quantity = dec.fromInt(10),
        .order_type = .limit,
        .stop_price = dec.fromInt(101),
    }));
    try testing.expectError(error.InvalidArgument, MarketOrder.init(.{
        .order_id = "bad-market",
        .symbol = "AAPL",
        .side = .buy,
        .quantity = dec.fromInt(10),
        .limit_price = dec.fromInt(101),
    }));
    try testing.expectError(error.InvalidArgument, MarketOrder.init(.{
        .order_id = "bad-stop-limit",
        .symbol = "AAPL",
        .side = .buy,
        .quantity = dec.fromInt(10),
        .order_type = .stop_limit,
        .limit_price = dec.fromInt(101),
    }));
    try testing.expectError(error.InvalidArgument, MarketOrder.init(.{
        .order_id = "zero",
        .symbol = "AAPL",
        .side = .buy,
        .quantity = 0,
    }));
}
