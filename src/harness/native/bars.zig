//! Exact OHLCV bar derivation from immutable trade events, mirroring
//! ``src/harness/data/bars.py``.
//!
//! Prices and volume remain the pinned i128 fixed-point type all the way
//! through aggregation so every bar reconciles exactly to its source trades.
//! Buckets are half-open ``[start, end)`` UTC intervals floored from the Unix
//! epoch; bars are ordered by interval start, and the trades inside one bar
//! are ordered by ``(timestamp, sequence, feed index)`` exactly like Python's
//! stable sort in ``_make_bar``.

const std = @import("std");
const market = @import("market.zig");

pub const dec = market.dec;

pub const Error = error{
    OutOfMemory,
    InvalidArgument,
    InvalidSymbol,
    InvalidDecimal,
    OutOfRange,
};

const TradeEvent = market.TradeEvent;

/// One non-empty, half-open OHLCV interval derived from trade events
/// (Python ``OHLCVBar``). ``start_ns`` is inclusive and ``end_ns`` exclusive.
pub const OHLCVBar = struct {
    symbol: []const u8,
    start_ns: i128,
    end_ns: i128,
    open: i128,
    high: i128,
    low: i128,
    close: i128,
    volume: i128,
    trade_count: usize,

    pub fn validate(self: OHLCVBar) Error!void {
        if (self.end_ns <= self.start_ns) return error.InvalidArgument;
        if (self.volume < 0) return error.InvalidArgument;
        if (self.high < self.low or
            self.open < self.low or self.open > self.high or
            self.close < self.low or self.close > self.high)
        {
            return error.InvalidArgument;
        }
        if (self.trade_count == 0) return error.InvalidArgument;
    }

    /// The bar start under the conventional OHLCV name.
    pub fn timestampNs(self: OHLCVBar) i128 {
        return self.start_ns;
    }
};

/// Normalize a configured bar interval to positive nanoseconds. Strings use
/// the harness configuration vocabulary: ``s``, ``m``/``min``, ``h``, and
/// ``d`` (Python ``interval_delta``; ``m`` is an input alias only).
pub fn intervalDeltaNs(interval: []const u8) Error!i128 {
    var buffer: [32]u8 = undefined;
    if (interval.len == 0 or interval.len > buffer.len) return error.InvalidArgument;
    for (interval, 0..) |byte, index| {
        buffer[index] = std.ascii.toLower(byte);
    }
    const value = std.mem.trim(u8, buffer[0..interval.len], " \t\r\n");
    var digits: usize = 0;
    while (digits < value.len and std.ascii.isDigit(value[digits])) : (digits += 1) {}
    const unit = value[digits..];
    if (digits == 0) return error.InvalidArgument;
    const seconds: i128 = if (std.mem.eql(u8, unit, "s"))
        1
    else if (std.mem.eql(u8, unit, "m") or std.mem.eql(u8, unit, "min"))
        60
    else if (std.mem.eql(u8, unit, "h"))
        3600
    else if (std.mem.eql(u8, unit, "d"))
        86_400
    else
        return error.InvalidArgument;
    const amount = std.fmt.parseInt(i64, value[0..digits], 10) catch return error.InvalidArgument;
    if (amount <= 0) return error.InvalidArgument;
    return std.math.mul(i128, @as(i128, amount), seconds * std.time.ns_per_s) catch error.OutOfRange;
}

/// Floor ``value_ns`` into its half-open bucket start (Python
/// ``_bucket_start``; the Unix epoch is the origin).
pub fn bucketStartNs(value_ns: i128, interval_ns: i128) i128 {
    return @divFloor(value_ns, interval_ns) * interval_ns;
}

const Bucket = struct {
    start_ns: i128,
    trades: std.ArrayList(TradeEvent) = .empty,
};

/// Accumulate one symbol's trades into deterministic OHLCV bars (Python
/// ``BarAggregator``).
pub const BarAggregator = struct {
    gpa: std.mem.Allocator,
    interval_ns: i128,
    symbol: ?[]const u8 = null,
    buckets: std.ArrayList(Bucket) = .empty,

    pub fn init(gpa: std.mem.Allocator, interval_ns: i128, symbol: ?[]const u8) Error!BarAggregator {
        if (interval_ns <= 0) return error.InvalidArgument;
        return .{ .gpa = gpa, .interval_ns = interval_ns, .symbol = symbol };
    }

    pub fn deinit(self: *BarAggregator) void {
        for (self.buckets.items) |*bucket| bucket.trades.deinit(self.gpa);
        self.buckets.deinit(self.gpa);
        self.buckets = .empty;
    }

    /// Add one trade without consulting any external clock or data source.
    pub fn add(self: *BarAggregator, trade: TradeEvent) Error!void {
        try trade.validate();
        if (self.symbol == null) {
            self.symbol = trade.symbol;
        } else if (!std.mem.eql(u8, self.symbol.?, trade.symbol)) {
            return error.InvalidArgument;
        }
        const start = bucketStartNs(trade.timestamp_ns, self.interval_ns);
        for (self.buckets.items) |*bucket| {
            if (bucket.start_ns == start) {
                try bucket.trades.append(self.gpa, trade);
                return;
            }
        }
        try self.buckets.append(self.gpa, .{ .start_ns = start });
        try self.buckets.items[self.buckets.items.len - 1].trades.append(self.gpa, trade);
    }

    /// Add trades in source-feed order.
    pub fn addMany(self: *BarAggregator, trades: []const TradeEvent) Error!void {
        for (trades) |trade| try self.add(trade);
    }

    /// Add ``trades`` and return the resulting bars.
    pub fn aggregate(self: *BarAggregator, trades: []const TradeEvent, out: *std.ArrayList(OHLCVBar)) Error!void {
        try self.addMany(trades);
        return self.finalize(out);
    }

    /// Return non-empty bars ordered by UTC interval start. The caller owns
    /// ``out``.
    pub fn finalize(self: *BarAggregator, out: *std.ArrayList(OHLCVBar)) Error!void {
        std.mem.sort(Bucket, self.buckets.items, {}, bucketLess);
        for (self.buckets.items) |*bucket| {
            try out.append(self.gpa, try makeBar(self.gpa, bucket.start_ns, self.interval_ns, bucket.trades.items));
        }
    }

    fn bucketLess(_: void, a: Bucket, b: Bucket) bool {
        return a.start_ns < b.start_ns;
    }
};

fn makeBar(gpa: std.mem.Allocator, start_ns: i128, interval_ns: i128, trades: []const TradeEvent) Error!OHLCVBar {
    if (trades.len == 0) return error.InvalidArgument;
    var indices: std.ArrayList(usize) = .empty;
    defer indices.deinit(gpa);
    try indices.ensureTotalCapacity(gpa, trades.len);
    for (0..trades.len) |index| indices.appendAssumeCapacity(index);
    // Sorting indices avoids shuffling the caller's feed order; the key
    // ``(timestamp, sequence, index)`` mirrors Python's stable sort exactly.
    sortIndices(indices.items, trades);
    return renderBar(start_ns, interval_ns, trades, indices.items);
}

fn sortIndices(indices: []usize, trades: []const TradeEvent) void {
    std.mem.sort(usize, indices, trades, struct {
        fn less(t: []const TradeEvent, a: usize, b: usize) bool {
            if (t[a].timestamp_ns != t[b].timestamp_ns) return t[a].timestamp_ns < t[b].timestamp_ns;
            if (t[a].sequence != t[b].sequence) return t[a].sequence < t[b].sequence;
            return a < b;
        }
    }.less);
}

fn renderBar(
    start_ns: i128,
    interval_ns: i128,
    trades: []const TradeEvent,
    ordered: []const usize,
) Error!OHLCVBar {
    var high = trades[ordered[0]].price;
    var low = trades[ordered[0]].price;
    var volume: i128 = 0;
    for (ordered) |index| {
        const price = trades[index].price;
        if (price > high) high = price;
        if (price < low) low = price;
        volume = try dec.add(volume, trades[index].quantity);
    }
    const bar = OHLCVBar{
        .symbol = trades[ordered[0]].symbol,
        .start_ns = start_ns,
        .end_ns = start_ns + interval_ns,
        .open = trades[ordered[0]].price,
        .high = high,
        .low = low,
        .close = trades[ordered[ordered.len - 1]].price,
        .volume = volume,
        .trade_count = ordered.len,
    };
    try bar.validate();
    return bar;
}

/// Build exact bars from a finite trade slice (Python ``build_bars``).
pub fn buildBars(
    gpa: std.mem.Allocator,
    trades: []const TradeEvent,
    interval_ns: i128,
    symbol: ?[]const u8,
    out: *std.ArrayList(OHLCVBar),
) Error!void {
    var aggregator = try BarAggregator.init(gpa, interval_ns, symbol);
    defer aggregator.deinit();
    try aggregator.addMany(trades);
    return aggregator.finalize(out);
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/data/test_bars.py: STAMP is
// 2024-01-19T14:30:00Z and every bar reconciles exactly to its trades.
// ---------------------------------------------------------------------------

const testing = std.testing;

const stamp_ns: i128 = 1_705_674_600_000_000_000;
const minute_ns: i128 = 60 * std.time.ns_per_s;

fn goldenTrade(event_id: []const u8, offset_seconds: i64, sequence: u64, price: i128, quantity: i128) TradeEvent {
    return .{
        .event_id = event_id,
        .timestamp_ns = stamp_ns + @as(i128, offset_seconds) * std.time.ns_per_s,
        .sequence = sequence,
        .symbol = "AAPL",
        .price = price,
        .quantity = quantity,
    };
}

test "golden bars reconcile prices volume and boundaries exactly" {
    const trades = [_]TradeEvent{
        goldenTrade("t3", 61, 3, dec.fromInt(101) + 250_000_000_000_000_000, dec.fromInt(0) + 750_000_000_000_000_000),
        goldenTrade("t1", 0, 1, dec.fromInt(100) + 100_000_000_000_000_000, dec.fromInt(2) + 500_000_000_000_000_000),
        goldenTrade("t2", 30, 2, dec.fromInt(99) + 900_000_000_000_000_000, dec.fromInt(1) + 250_000_000_000_000_000),
        goldenTrade("t4", 120, 4, dec.fromInt(102), dec.fromInt(3)),
    };

    var bars: std.ArrayList(OHLCVBar) = .empty;
    defer bars.deinit(testing.allocator);
    try buildBars(testing.allocator, &trades, minute_ns, null, &bars);

    try testing.expectEqual(@as(usize, 3), bars.items.len);
    const starts = [_]i128{ stamp_ns, stamp_ns + minute_ns, stamp_ns + 2 * minute_ns };
    const ends = [_]i128{ stamp_ns + minute_ns, stamp_ns + 2 * minute_ns, stamp_ns + 3 * minute_ns };
    for (bars.items, 0..) |bar, index| {
        try testing.expectEqual(starts[index], bar.start_ns);
        try testing.expectEqual(ends[index], bar.end_ns);
    }

    // Bar 0: open 100.10, high 100.10, low 99.90, close 99.90, volume 3.75.
    try testing.expectEqual(dec.fromInt(100) + 100_000_000_000_000_000, bars.items[0].open);
    try testing.expectEqual(dec.fromInt(100) + 100_000_000_000_000_000, bars.items[0].high);
    try testing.expectEqual(dec.fromInt(99) + 900_000_000_000_000_000, bars.items[0].low);
    try testing.expectEqual(dec.fromInt(99) + 900_000_000_000_000_000, bars.items[0].close);
    try testing.expectEqual(dec.fromInt(3) + 750_000_000_000_000_000, bars.items[0].volume);
    try testing.expectEqual(@as(usize, 2), bars.items[0].trade_count);

    // Bar 1: a single 101.25 trade of 0.75.
    try testing.expectEqual(dec.fromInt(101) + 250_000_000_000_000_000, bars.items[1].open);
    try testing.expectEqual(dec.fromInt(101) + 250_000_000_000_000_000, bars.items[1].close);
    try testing.expectEqual(dec.fromInt(0) + 750_000_000_000_000_000, bars.items[1].volume);
    try testing.expectEqual(@as(usize, 1), bars.items[1].trade_count);

    // Bar 2: a single 102.00 trade of 3.
    try testing.expectEqual(dec.fromInt(102), bars.items[2].open);
    try testing.expectEqual(dec.fromInt(102), bars.items[2].close);
    try testing.expectEqual(dec.fromInt(3), bars.items[2].volume);

    var total_volume: i128 = 0;
    var total_quantity: i128 = 0;
    for (bars.items) |bar| total_volume = try dec.add(total_volume, bar.volume);
    for (trades) |trade| total_quantity = try dec.add(total_quantity, trade.quantity);
    try testing.expectEqual(total_quantity, total_volume);
}

test "streaming aggregator keeps one symbol per instance" {
    var aggregator = try BarAggregator.init(testing.allocator, 5 * minute_ns, "AAPL");
    defer aggregator.deinit();
    try aggregator.addMany(&[_]TradeEvent{
        goldenTrade("t1", 0, 1, dec.fromInt(100), dec.one),
        goldenTrade("t2", 1, 2, dec.fromInt(101), dec.fromInt(2)),
    });

    var bars: std.ArrayList(OHLCVBar) = .empty;
    defer bars.deinit(testing.allocator);
    try aggregator.finalize(&bars);

    try testing.expectEqual(@as(usize, 1), bars.items.len);
    const bar = bars.items[0];
    try testing.expectEqual(stamp_ns, bar.timestampNs());
    try testing.expectEqual(dec.fromInt(100), bar.open);
    try testing.expectEqual(dec.fromInt(101), bar.close);
    try testing.expectEqual(dec.fromInt(3), bar.volume);
    try testing.expectEqualStrings("AAPL", aggregator.symbol.?);

    // Trades of another symbol are refused, mirroring the Python aggregator.
    const wrong_symbol = TradeEvent{
        .event_id = "t3",
        .timestamp_ns = stamp_ns,
        .sequence = 3,
        .symbol = "MSFT",
        .price = dec.one,
        .quantity = dec.one,
    };
    try testing.expectError(error.InvalidArgument, aggregator.add(wrong_symbol));
}

test "interval deltas parse the harness configuration vocabulary" {
    try testing.expectEqual(@as(i128, std.time.ns_per_s), try intervalDeltaNs("1s"));
    try testing.expectEqual(@as(i128, 5 * 60 * std.time.ns_per_s), try intervalDeltaNs("5min"));
    try testing.expectEqual(@as(i128, 60 * std.time.ns_per_s), try intervalDeltaNs("1m"));
    try testing.expectEqual(@as(i128, 60 * std.time.ns_per_s), try intervalDeltaNs(" 1MIN "));
    try testing.expectEqual(@as(i128, 3600 * std.time.ns_per_s), try intervalDeltaNs("1h"));
    try testing.expectEqual(@as(i128, 86_400 * std.time.ns_per_s), try intervalDeltaNs("1d"));
    try testing.expectError(error.InvalidArgument, intervalDeltaNs("0min"));
    try testing.expectError(error.InvalidArgument, intervalDeltaNs("min"));
    try testing.expectError(error.InvalidArgument, intervalDeltaNs("1w"));
    try testing.expectError(error.InvalidArgument, intervalDeltaNs(""));
    try testing.expectError(error.InvalidArgument, BarAggregator.init(testing.allocator, 0, null));
}

test "bucket boundaries are half-open and floored from the epoch" {
    // A trade exactly on a boundary opens the next bucket; a trade one
    // nanosecond before it closes the previous one.
    const boundary = bucketStartNs(stamp_ns, minute_ns);
    try testing.expectEqual(stamp_ns, boundary);
    try testing.expectEqual(stamp_ns + minute_ns, bucketStartNs(stamp_ns + minute_ns, minute_ns));
    try testing.expectEqual(stamp_ns, bucketStartNs(stamp_ns + minute_ns - 1, minute_ns));
    // Floor division handles pre-epoch instants like Python's ``//``.
    try testing.expectEqual(-minute_ns, bucketStartNs(-1, minute_ns));
}
