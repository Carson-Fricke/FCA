//! Deterministic fill models for single-leg orders, ported from
//! ``src/harness/fills``. This is the stable facade of the fills package:
//! ``simple_touch.zig`` (``SimpleTouch`` + the shared ``FillDecision``),
//! ``latency_size.zig`` (``LatencySize``), and ``trade_through.zig``
//! (``TradeThrough``) mirror their Python files; ``mid_touch`` and ``fees``
//! remain Python-side concerns.
//!
//! Every decision is an exact i128 fixed-point value at scale 18. Response
//! latency stays plugin metadata (integer microseconds) — the simulated venue
//! owns response scheduling (see ``matching.zig``). The stateful models track
//! cancel-remainder policy per order identifier; the caller still supplies the
//! authoritative remaining quantity on every invocation.

const std = @import("std");
const market = @import("market.zig");

pub const simple_touch = @import("simple_touch.zig");
pub const latency_size = @import("latency_size.zig");
pub const trade_through = @import("trade_through.zig");

pub const dec = market.dec;
pub const Side = market.Side;
pub const OrderType = market.OrderType;
pub const Error = market.Error;

pub const Liquidity = simple_touch.Liquidity;
pub const RemainderPolicy = simple_touch.RemainderPolicy;
pub const FillDecision = simple_touch.FillDecision;
pub const DecideOptions = simple_touch.DecideOptions;
pub const SimpleTouch = simple_touch.SimpleTouch;
pub const LatencySizeConfig = latency_size.LatencySizeConfig;
pub const LatencyMetadata = latency_size.LatencyMetadata;
pub const LatencySize = latency_size.LatencySize;
pub const TradeThroughConfig = trade_through.TradeThroughConfig;
pub const TradeThroughMetadata = trade_through.TradeThroughMetadata;
pub const TradeThrough = trade_through.TradeThrough;

const MarketEvent = market.MarketEvent;
const MarketOrder = market.MarketOrder;

/// The fill plugin interface consumed by the simulated venue (Python's
/// ``FillPlugin`` callable). ``SimpleTouch`` is the venue default; the
/// stateful models are passed by pointer so their cancel-remainder state is
/// shared with the caller.
pub const FillModel = union(enum) {
    simple_touch: SimpleTouch,
    latency_size: *LatencySize,
    trade_through: *TradeThrough,

    pub fn decide(
        self: FillModel,
        order: MarketOrder,
        snap: MarketEvent,
        opts: DecideOptions,
    ) Error!?FillDecision {
        return switch (self) {
            .simple_touch => |model| model.decide(order, snap, opts),
            .latency_size => |model| model.decide(order, snap, opts),
            .trade_through => |model| model.decide(order, snap, opts),
        };
    }

    pub fn isSimpleTouch(self: FillModel) bool {
        return self == .simple_touch;
    }
};

comptime {
    _ = simple_touch;
    _ = latency_size;
    _ = trade_through;
}

// ---------------------------------------------------------------------------
// Tests. The plugin interface dispatches by model kind exactly like the
// Python venue's callable-plugin protocol.
// ---------------------------------------------------------------------------

const testing = std.testing;
const st = simple_touch;

test "fill model interface dispatches by plugin kind" {
    var latency = LatencySize.init(testing.allocator, .{});
    defer latency.deinit();
    const buy = st.makeOrder("interface", .buy, .market, st.ten, null, null);
    const via_touch = try (FillModel{ .simple_touch = .{} }).decide(buy, st.defaultQuote("q"), .{});
    try st.expectDecision(via_touch, st.ten, st.one0one, .taker);
    const via_latency = try (FillModel{ .latency_size = &latency }).decide(buy, st.defaultQuote("q"), .{});
    try st.expectDecision(via_latency, st.ten, st.one0one, .taker);
    try testing.expect((FillModel{ .simple_touch = .{} }).isSimpleTouch());
}
