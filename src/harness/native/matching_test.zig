//! Golden vectors for the simulated venue, mirrored from
//! ``tests/harness/venues/test_sim.py``. STAMP is 2024-01-19T14:30:00Z and
//! every scenario drives the same publish → dispatch → submit flow as the
//! Python tests through the deterministic event bus; ``seen`` collects the
//! dispatched execution-report and fill identities exactly like the Python
//! ``seen`` subscription list.

const std = @import("std");
const bus = @import("bus.zig");
const clock_mod = @import("clock.zig");
const market = @import("market.zig");
const matching = @import("matching.zig");
const oms = @import("oms.zig");

const testing = std.testing;
const dec = oms.dec;

const stamp_ns: i128 = 1_705_674_600_000_000_000;
const micro: i128 = 1_000;
const hundred = dec.fromInt(100);
const one0one = dec.fromInt(101);
const ten = dec.fromInt(10);

const Harness = struct {
    gpa: std.mem.Allocator,
    sim: clock_mod.SimClock,
    event_bus: bus.EventBus,
    venue: matching.SimulatedVenue,
    seen: std.ArrayList([]const u8) = .empty,

    fn init(self: *Harness, gpa: std.mem.Allocator, latency_ns: i128) matching.Error!void {
        self.* = .{
            .gpa = gpa,
            .sim = clock_mod.SimClock.init(stamp_ns),
            .event_bus = undefined,
            .venue = undefined,
        };
        self.event_bus = bus.EventBus.init(gpa, &self.sim);
        errdefer self.event_bus.deinit();
        try matching.SimulatedVenue.init(
            gpa,
            &self.event_bus,
            .{ .simple_touch = .{} },
            latency_ns,
            &self.sim,
            &self.venue,
        );
        try self.event_bus.subscribe(.{ .event_type = .execution_report }, recordSeen, self);
        try self.event_bus.subscribe(.{ .event_type = .fill }, recordSeen, self);
    }

    fn deinit(self: *Harness) void {
        self.venue.deinit();
        self.event_bus.deinit();
        self.seen.deinit(self.gpa);
    }

    fn recordSeen(context: ?*anyopaque, event: *const bus.Event) void {
        const self: *Harness = @ptrCast(@alignCast(context.?));
        self.seen.append(self.gpa, event.event_id) catch @panic("test append failed");
    }

    /// Publish the market envelope and route the payload to the venue (the
    /// native bus envelope carries no payload; payload routing happens at
    /// dispatch time, before the queue drains).
    fn feed(self: *Harness, event: market.MarketEvent) matching.Error!void {
        try event.validate();
        try self.event_bus.publish(bus.Event.initDefault(
            event.eventType(),
            event.timestampNs(),
            event.sequence(),
            event.eventId(),
        ));
        try self.venue.onMarket(event);
    }

    fn drain(self: *Harness) matching.Error!usize {
        return self.event_bus.drain(null);
    }
};

fn quote(
    event_id: []const u8,
    offset_us: i128,
    sequence: u64,
    bid: i128,
    ask: i128,
    bid_quantity: i128,
    ask_quantity: i128,
) market.MarketEvent {
    return .{ .quote = .{
        .event_id = event_id,
        .timestamp_ns = stamp_ns + offset_us * micro,
        .sequence = sequence,
        .symbol = "AAPL",
        .bid_price = bid,
        .bid_quantity = bid_quantity,
        .ask_price = ask,
        .ask_quantity = ask_quantity,
    } };
}

fn defaultQuote(event_id: []const u8) market.MarketEvent {
    return quote(event_id, 0, 0, hundred, one0one, ten, ten);
}

fn order(
    order_id: []const u8,
    side: oms.Side,
    order_type: oms.OrderType,
    quantity: i128,
    time_in_force: oms.TimeInForce,
    limit_price: ?i128,
    stop_price: ?i128,
    offset_us: i128,
) market.MarketOrder {
    return .{
        .order_id = order_id,
        .symbol = "AAPL",
        .side = side,
        .quantity = quantity,
        .order_type = order_type,
        .time_in_force = time_in_force,
        .limit_price = limit_price,
        .stop_price = stop_price,
        .timestamp_ns = stamp_ns + offset_us * micro,
    };
}

test "buy and sell crossing scenarios emit ack reports and fills via the bus" {
    var harness: Harness = undefined;
    try harness.init(testing.allocator, 0);
    defer harness.deinit();

    try harness.feed(defaultQuote("q"));
    try testing.expectEqual(@as(usize, 1), try harness.drain());

    try harness.venue.submit(order("buy", .buy, .limit, ten, .day, one0one, null, 0));
    try harness.venue.submit(order("sell", .sell, .limit, ten, .day, hundred, null, 0));
    // Outcomes are scheduled but not dispatched yet (Python ``seen == []``).
    try testing.expectEqual(@as(u64, 1), harness.event_bus.dispatchedCount());

    _ = try harness.drain();

    const reports = harness.venue.report_records.items;
    try testing.expectEqual(@as(usize, 4), reports.len);
    try testing.expectEqual(oms.ExecutionType.acknowledged, reports[0].execution_type);
    try testing.expectEqual(oms.ExecutionType.fill, reports[1].execution_type);
    try testing.expectEqual(oms.ExecutionType.acknowledged, reports[2].execution_type);
    try testing.expectEqual(oms.ExecutionType.fill, reports[3].execution_type);

    const fill_records = harness.venue.fill_records.items;
    try testing.expectEqual(@as(usize, 2), fill_records.len);
    try testing.expectEqualStrings("buy", fill_records[0].order_id);
    try testing.expectEqual(oms.Side.buy, fill_records[0].side);
    try testing.expectEqual(one0one, fill_records[0].price);
    try testing.expectEqualStrings("sell", fill_records[1].order_id);
    try testing.expectEqual(oms.Side.sell, fill_records[1].side);
    try testing.expectEqual(hundred, fill_records[1].price);

    for (reports) |report| try testing.expectEqual(stamp_ns, report.timestamp_ns);
    for (fill_records) |fill| try testing.expectEqual(stamp_ns, fill.timestamp_ns);
    try testing.expectEqual(stamp_ns, harness.sim.now());

    // Dispatch order is exactly the deterministic queue order.
    try testing.expectEqual(@as(usize, 6), harness.seen.items.len);
    try testing.expectEqualStrings(reports[0].event_id, harness.seen.items[0]);
    try testing.expectEqualStrings(reports[1].event_id, harness.seen.items[1]);
    try testing.expectEqualStrings(fill_records[0].event_id, harness.seen.items[2]);
    try testing.expectEqualStrings(reports[2].event_id, harness.seen.items[3]);
    try testing.expectEqualStrings(reports[3].event_id, harness.seen.items[4]);
    try testing.expectEqualStrings(fill_records[1].event_id, harness.seen.items[5]);
}

test "resting orders fill only when a later quote touches" {
    var harness: Harness = undefined;
    try harness.init(testing.allocator, 0);
    defer harness.deinit();

    // BUY limit 100 rests below the initial offer and fills at the touch.
    try harness.feed(quote("resting", 0, 0, dec.fromInt(99), one0one, ten, ten));
    _ = try harness.drain();
    try harness.venue.submit(order("rest-buy", .buy, .limit, ten, .day, hundred, null, 0));
    _ = try harness.drain();
    try testing.expectEqual(@as(usize, 0), harness.venue.fill_records.items.len);

    try harness.feed(quote("touch", 1, 1, dec.fromInt(99), hundred, ten, ten));
    _ = try harness.drain();
    try testing.expectEqual(@as(usize, 1), harness.venue.fill_records.items.len);
    try testing.expectEqual(hundred, harness.venue.fill_records.items[0].price);

    // SELL limit 101 rests above the initial bid and fills at the touch.
    var sell_harness: Harness = undefined;
    try sell_harness.init(testing.allocator, 0);
    defer sell_harness.deinit();

    try sell_harness.feed(quote("resting", 0, 0, hundred, dec.fromInt(102), ten, ten));
    _ = try sell_harness.drain();
    try sell_harness.venue.submit(order("rest-sell", .sell, .limit, ten, .day, one0one, null, 0));
    _ = try sell_harness.drain();
    try testing.expectEqual(@as(usize, 0), sell_harness.venue.fill_records.items.len);

    try sell_harness.feed(quote("touch", 1, 1, one0one, dec.fromInt(102), ten, ten));
    _ = try sell_harness.drain();
    try testing.expectEqual(@as(usize, 1), sell_harness.venue.fill_records.items.len);
    try testing.expectEqual(one0one, sell_harness.venue.fill_records.items[0].price);
}

test "stop orders arm then trigger with the triggering snapshot" {
    var harness: Harness = undefined;
    try harness.init(testing.allocator, 0);
    defer harness.deinit();

    try harness.feed(quote("before", 0, 0, hundred, one0one, ten, ten));
    _ = try harness.drain();
    try harness.venue.submit(order("stop-buy", .buy, .stop, ten, .day, null, one0one, 0));
    _ = try harness.drain();
    try testing.expect((try harness.venue.state("stop-buy")).armed);
    try testing.expectEqual(@as(usize, 0), harness.venue.fill_records.items.len);

    try harness.feed(quote("trigger", 1, 1, hundred, one0one, ten, ten));
    _ = try harness.drain();

    const state = try harness.venue.state("stop-buy");
    try testing.expect(!state.armed);
    try testing.expect(state.triggering_market != null);
    try testing.expectEqualStrings("trigger", state.triggering_market.?.eventId());
    try testing.expectEqual(one0one, state.decision_price.?);
    try testing.expectEqual(@as(usize, 1), harness.venue.fill_records.items.len);
    try testing.expectEqual(one0one, harness.venue.fill_records.items[0].price);

    // SELL stop arms on the bid touch and fills at the bid.
    var sell_harness: Harness = undefined;
    try sell_harness.init(testing.allocator, 0);
    defer sell_harness.deinit();

    try sell_harness.feed(quote("before", 0, 0, one0one, dec.fromInt(102), ten, ten));
    _ = try sell_harness.drain();
    try sell_harness.venue.submit(order("stop-sell", .sell, .stop, ten, .day, null, hundred, 0));
    _ = try sell_harness.drain();
    try testing.expect((try sell_harness.venue.state("stop-sell")).armed);

    try sell_harness.feed(quote("trigger", 1, 1, hundred, one0one, ten, ten));
    _ = try sell_harness.drain();

    const sell_state = try sell_harness.venue.state("stop-sell");
    try testing.expect(!sell_state.armed);
    try testing.expectEqualStrings("trigger", sell_state.triggering_market.?.eventId());
    try testing.expectEqual(hundred, sell_state.decision_price.?);
    try testing.expectEqual(hundred, sell_harness.venue.fill_records.items[0].price);
}

test "partial fill then worsening quotes never improve the fill" {
    var harness: Harness = undefined;
    try harness.init(testing.allocator, 0);
    defer harness.deinit();

    try harness.venue.submit(order("partial", .buy, .limit, ten, .day, one0one, null, 0));
    try harness.feed(quote("first", 0, 1, hundred, one0one, ten, dec.fromInt(3)));
    _ = try harness.drain();
    try testing.expectEqual(@as(usize, 1), harness.venue.fill_records.items.len);
    try testing.expectEqual(dec.fromInt(3), harness.venue.fill_records.items[0].quantity);

    try harness.feed(quote("worse", 1, 2, hundred, dec.fromInt(102), ten, ten));
    _ = try harness.drain();
    try testing.expectEqual(@as(usize, 1), harness.venue.fill_records.items.len);

    try harness.feed(quote("better", 2, 3, hundred, hundred, ten, dec.fromInt(7)));
    _ = try harness.drain();
    const fill_records = harness.venue.fill_records.items;
    try testing.expectEqual(@as(usize, 2), fill_records.len);
    try testing.expectEqual(dec.fromInt(3), fill_records[0].quantity);
    try testing.expectEqual(one0one, fill_records[0].price);
    try testing.expectEqual(dec.fromInt(7), fill_records[1].quantity);
    try testing.expectEqual(hundred, fill_records[1].price);
}

test "stop limit arms on trigger and can rest after a non-crossing trigger" {
    var harness: Harness = undefined;
    try harness.init(testing.allocator, 0);
    defer harness.deinit();

    try harness.venue.submit(order(
        "stop-limit",
        .buy,
        .stop_limit,
        ten,
        .day,
        hundred + 500_000_000_000_000_000,
        one0one,
        0,
    ));
    try harness.feed(quote("trigger", 0, 1, hundred, one0one, ten, ten));
    _ = try harness.drain();
    const state = try harness.venue.state("stop-limit");
    try testing.expect(state.triggering_market != null);
    try testing.expectEqualStrings("trigger", state.triggering_market.?.eventId());
    try testing.expectEqual(@as(usize, 0), harness.venue.fill_records.items.len);

    try harness.feed(quote("touch", 1, 2, hundred, hundred + 500_000_000_000_000_000, ten, ten));
    _ = try harness.drain();
    try testing.expectEqual(@as(usize, 1), harness.venue.fill_records.items.len);
}

test "cancellation and replacement outcomes are scheduled" {
    var harness: Harness = undefined;
    try harness.init(testing.allocator, 0);
    defer harness.deinit();

    try harness.venue.submit(order("cancel", .buy, .limit, ten, .day, dec.fromInt(99), null, 0));
    _ = try harness.drain();
    try harness.venue.cancel("cancel");
    _ = try harness.drain();
    try testing.expectEqual(oms.OrderStatus.canceled, (try harness.venue.state("cancel")).status);

    try harness.venue.submit(order("original", .buy, .limit, ten, .day, dec.fromInt(99), null, 0));
    _ = try harness.drain();
    try harness.feed(quote("replacement-market", 1, 1, hundred, one0one, ten, ten));
    _ = try harness.drain();
    try harness.venue.replace("original", order(
        "replacement",
        .buy,
        .limit,
        dec.fromInt(4),
        .day,
        one0one,
        null,
        0,
    ));
    try testing.expectEqual(oms.OrderStatus.replaced, (try harness.venue.state("original")).status);
    _ = try harness.drain();
    try testing.expectEqual(oms.OrderStatus.filled, (try harness.venue.state("replacement")).status);

    var has_replaced_report = false;
    for (harness.venue.report_records.items) |report| {
        if (report.execution_type == .replaced) has_replaced_report = true;
    }
    try testing.expect(has_replaced_report);
}

test "latency preserves quote ack report fill ordering and timestamps" {
    var harness: Harness = undefined;
    try harness.init(testing.allocator, 25 * micro);
    defer harness.deinit();

    try harness.venue.submit(order("delayed", .buy, .market, ten, .day, null, null, 0));
    try harness.feed(quote("quote", 0, 1, hundred, one0one, ten, ten));
    try testing.expectEqual(@as(usize, 0), harness.seen.items.len);
    try testing.expectEqual(@as(usize, 4), try harness.drain());

    // Outcomes: acknowledgement, fill report, fill — in that order, all at
    // head + latency.
    const reports = harness.venue.report_records.items;
    const fill_records = harness.venue.fill_records.items;
    try testing.expectEqual(@as(usize, 2), reports.len);
    try testing.expectEqual(@as(usize, 1), fill_records.len);
    try testing.expectEqual(oms.ExecutionType.acknowledged, reports[0].execution_type);
    try testing.expectEqual(oms.ExecutionType.fill, reports[1].execution_type);
    for (reports) |report| try testing.expectEqual(stamp_ns + 25 * micro, report.timestamp_ns);
    try testing.expectEqual(stamp_ns + 25 * micro, fill_records[0].timestamp_ns);
    try testing.expectEqual(one0one, fill_records[0].price);
    try testing.expect(harness.venue.latest_market != null);

    // The dispatched identities match the scheduled outcome records exactly.
    try testing.expectEqual(@as(usize, 3), harness.seen.items.len);
    try testing.expectEqualStrings(reports[0].event_id, harness.seen.items[0]);
    try testing.expectEqualStrings(reports[1].event_id, harness.seen.items[1]);
    try testing.expectEqualStrings(fill_records[0].event_id, harness.seen.items[2]);
}

test "ioc non-fill expires through the bus" {
    var harness: Harness = undefined;
    try harness.init(testing.allocator, 0);
    defer harness.deinit();

    try harness.venue.submit(order("ioc", .buy, .limit, ten, .ioc, dec.fromInt(99), null, 0));
    try harness.feed(quote("opportunity", 0, 1, hundred, one0one, ten, ten));
    _ = try harness.drain();

    const reports = harness.venue.report_records.items;
    try testing.expectEqual(@as(usize, 2), reports.len);
    try testing.expectEqual(oms.OrderStatus.accepted, reports[0].status);
    try testing.expectEqual(oms.OrderStatus.expired, reports[1].status);
    try testing.expectEqual(oms.OrderStatus.expired, (try harness.venue.state("ioc")).status);
}

test "commands do not inline outcomes or allow pending races" {
    var harness: Harness = undefined;
    try harness.init(testing.allocator, 1 * micro);
    defer harness.deinit();

    try harness.venue.submit(order("pending", .buy, .market, ten, .day, null, null, 0));
    try harness.feed(defaultQuote("q"));
    _ = try harness.event_bus.dispatchNext();
    try testing.expectEqual(@as(u64, 1), harness.event_bus.dispatchedCount());
    try testing.expectEqual(@as(usize, 0), harness.seen.items.len);
    try testing.expectError(error.VenueState, harness.venue.cancel("pending"));
}
