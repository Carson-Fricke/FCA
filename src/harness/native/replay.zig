//! K-way deterministic replay merge, ported from
//! ``src/harness/data/replay.py``.
//!
//! Ordering contract (mirrored exactly from the Python ``_HeapItem`` and the
//! native ``bus.zig`` kernel): events are merged by timestamp, then source
//! sequence, then the serialized event-class priority table
//! (``bus.EVENT_CLASS_PRIORITY``, where quotes precede trades at equal
//! keys), then the source feed order, then the source-local row order.
//!
//! Each source is a one-lookahead cursor that must itself be ordered by
//! timestamp and sequence (Python raises ``ValueError`` otherwise; here
//! ``error.RetrogradeSource``). Sources are optionally pinned to one session
//! day and one data type; events whose symbol or type disagree with their
//! source are refused. Sessions are half-open ``[open, close)`` windows;
//! cursors persist across sessions so events before a session open are
//! permanently consumed, exactly like the Python feed.
//!
//! Sources consume already-normalized market events; ``batchSource`` decodes
//! one Arrow C Data Interface batch (see ``arrow_batch.zig``) into a source.

const std = @import("std");
const arrow = @import("arrow.zig");
const arrow_batch = @import("arrow_batch.zig");
const bus = @import("bus.zig");
const market = @import("market.zig");

pub const Error = error{
    OutOfMemory,
    InvalidArgument,
    InvalidSymbol,
    InvalidDecimal,
    InvalidArrow,
    OutOfRange,
    /// Each replay source must be ordered by timestamp and sequence.
    RetrogradeSource,
    /// Replay event symbol does not match its source.
    SymbolMismatch,
    /// Replay event type does not match its source data type.
    EventTypeMismatch,
};

const MarketEvent = market.MarketEvent;

/// One explicit half-open replay session (Python ``ExchangeSession`` as seen
/// by the replay feed). ``day`` is the session date in days since the Unix
/// epoch and only drives source-partition filtering; the window is defined
/// by the timestamps alone.
pub const Session = struct {
    day: i64,
    open_ns: i128,
    close_ns: i128,

    pub fn init(day: i64, open_ns: i128, close_ns: i128) Error!Session {
        if (close_ns <= open_ns) return error.InvalidArgument;
        return .{ .day = day, .open_ns = open_ns, .close_ns = close_ns };
    }

    /// The half-open UTC day ``[00:00, 24:00)`` for one date (Python's bare
    /// ``date`` session shorthand).
    pub fn utcDay(day: i64) Session {
        const ns_per_day: i128 = 86_400 * std.time.ns_per_s;
        const open = @as(i128, day) * ns_per_day;
        return .{ .day = day, .open_ns = open, .close_ns = open + ns_per_day };
    }
};

/// The ordering key of one source event (Python's cursor key tuple).
pub const SourceKey = struct {
    timestamp_ns: i128,
    sequence: u64,
    class_priority: u8,
};

fn keyLess(a: SourceKey, b: SourceKey) bool {
    if (a.timestamp_ns != b.timestamp_ns) return a.timestamp_ns < b.timestamp_ns;
    if (a.sequence != b.sequence) return a.sequence < b.sequence;
    return a.class_priority < b.class_priority;
}

/// A one-lookahead cursor over one explicitly ordered source (Python
/// ``_Cursor``). The event slice is borrowed and must outlive the feed.
pub const EventSource = struct {
    symbol: []const u8,
    expected_type: ?bus.EventType = null,
    session_day: ?i64 = null,
    events: []const MarketEvent,
    index: usize = 0,
    last_key: ?SourceKey = null,
    validated_current: bool = false,

    pub fn init(
        symbol: []const u8,
        expected_type: ?bus.EventType,
        session_day: ?i64,
        events: []const MarketEvent,
    ) Error!EventSource {
        if (symbol.len == 0) return error.InvalidArgument;
        return .{
            .symbol = symbol,
            .expected_type = expected_type,
            .session_day = session_day,
            .events = events,
        };
    }

    /// Return the current lookahead event without consuming it, validating
    /// the source ordering lazily like Python's ``_advance``.
    pub fn peek(self: *EventSource) Error!?MarketEvent {
        if (self.index >= self.events.len) return null;
        if (!self.validated_current) {
            const event = self.events[self.index];
            const key = SourceKey{
                .timestamp_ns = event.timestampNs(),
                .sequence = event.sequence(),
                .class_priority = event.classPriority(),
            };
            if (self.last_key) |last| {
                if (keyLess(key, last)) return error.RetrogradeSource;
            }
            self.last_key = key;
            self.validated_current = true;
        }
        return self.events[self.index];
    }

    /// Consume and return the current event (Python ``pop``).
    pub fn pop(self: *EventSource) Error!MarketEvent {
        const event = (try self.peek()) orelse return error.InvalidArgument;
        self.index += 1;
        self.validated_current = false;
        return event;
    }

    /// The source-local ordinal of the current event (Python ``row_order``).
    pub fn rowOrder(self: *const EventSource) usize {
        return self.index;
    }
};

const HeapItem = struct {
    key: SourceKey,
    source_order: usize,
    row_order: usize,
    event: MarketEvent,
};

fn itemLess(a: HeapItem, b: HeapItem) bool {
    if (keyLess(a.key, b.key)) return true;
    if (keyLess(b.key, a.key)) return false;
    if (a.source_order != b.source_order) return a.source_order < b.source_order;
    return a.row_order < b.row_order;
}

fn heapPush(gpa: std.mem.Allocator, heap: *std.ArrayList(HeapItem), item: HeapItem) Error!void {
    try heap.append(gpa, item);
    const items = heap.items;
    var child_index = items.len - 1;
    while (child_index > 0) {
        const parent = (child_index - 1) / 2;
        if (!itemLess(items[child_index], items[parent])) break;
        std.mem.swap(HeapItem, &items[child_index], &items[parent]);
        child_index = parent;
    }
}

fn heapPop(heap: *std.ArrayList(HeapItem)) HeapItem {
    const root = heap.items[0];
    const last = heap.pop().?;
    // Re-read the items slice after the pop: the sift-down must cover the
    // reduced length, not the pre-pop length.
    const items = heap.items;
    if (items.len > 0) {
        items[0] = last;
        var index: usize = 0;
        while (true) {
            const left = 2 * index + 1;
            const right = 2 * index + 2;
            var smallest = index;
            if (left < items.len and itemLess(items[left], items[smallest])) smallest = left;
            if (right < items.len and itemLess(items[right], items[smallest])) smallest = right;
            if (smallest == index) break;
            std.mem.swap(HeapItem, &items[index], &items[smallest]);
            index = smallest;
        }
    }
    return root;
}

/// Stream deterministic market events one explicit session at a time
/// (Python ``LakeReplayFeed`` over normalized event streams). Sources keep
/// their submission (feed) order, which is the fourth merge key.
pub const ReplayFeed = struct {
    gpa: std.mem.Allocator,
    sources: std.ArrayList(EventSource) = .empty,
    sessions: std.ArrayList(Session) = .empty,

    pub fn init(gpa: std.mem.Allocator) ReplayFeed {
        return .{ .gpa = gpa };
    }

    pub fn deinit(self: *ReplayFeed) void {
        self.sources.deinit(self.gpa);
        self.sessions.deinit(self.gpa);
        self.sources = .empty;
        self.sessions = .empty;
    }

    /// Add one source in feed order.
    pub fn addSource(self: *ReplayFeed, source: EventSource) Error!void {
        try self.sources.append(self.gpa, source);
    }

    /// Add one session; sessions are kept sorted by date and must not
    /// duplicate a date (Python ``_normalize_sessions``).
    pub fn addSession(self: *ReplayFeed, session: Session) Error!void {
        var index: usize = 0;
        while (index < self.sessions.items.len) : (index += 1) {
            if (self.sessions.items[index].day == session.day) return error.InvalidArgument;
            if (self.sessions.items[index].day > session.day) break;
        }
        try self.sessions.insert(self.gpa, index, session);
    }

    /// Merge one session into ``out`` without collecting the other sessions.
    pub fn mergeSession(self: *ReplayFeed, session: Session, out: *std.ArrayList(MarketEvent)) Error!void {
        var heap: std.ArrayList(HeapItem) = .empty;
        defer heap.deinit(self.gpa);

        for (self.sources.items, 0..) |*source, order| {
            if (source.session_day) |day| {
                if (day != session.day) continue;
            }
            var current = (try source.peek()) orelse continue;
            while (current.timestampNs() < session.open_ns) {
                _ = try source.pop();
                current = (try source.peek()) orelse break;
            }
            if (current.timestampNs() >= session.open_ns and current.timestampNs() < session.close_ns) {
                try validateSourceEvent(source, current);
                try heapPush(self.gpa, &heap, .{
                    .key = .{
                        .timestamp_ns = current.timestampNs(),
                        .sequence = current.sequence(),
                        .class_priority = current.classPriority(),
                    },
                    .source_order = order,
                    .row_order = source.rowOrder(),
                    .event = current,
                });
            }
        }

        while (heap.items.len > 0) {
            const item = heapPop(&heap);
            const source = &self.sources.items[item.source_order];
            _ = try source.pop();
            try out.append(self.gpa, item.event);
            const next = (try source.peek()) orelse continue;
            if (next.timestampNs() < session.close_ns) {
                try validateSourceEvent(source, next);
                try heapPush(self.gpa, &heap, .{
                    .key = .{
                        .timestamp_ns = next.timestampNs(),
                        .sequence = next.sequence(),
                        .class_priority = next.classPriority(),
                    },
                    .source_order = item.source_order,
                    .row_order = source.rowOrder(),
                    .event = next,
                });
            }
        }
    }

    /// Merge every session in chronological order into ``out``.
    pub fn mergeAll(self: *ReplayFeed, out: *std.ArrayList(MarketEvent)) Error!void {
        for (self.sessions.items) |session| {
            try self.mergeSession(session, out);
        }
    }

    fn validateSourceEvent(source: *const EventSource, event: MarketEvent) Error!void {
        if (!std.mem.eql(u8, event.symbol(), source.symbol)) return error.SymbolMismatch;
        if (source.expected_type) |expected| {
            if (event.eventType() != expected) return error.EventTypeMismatch;
        }
    }
};

/// Decode one Arrow market batch and wrap it as a replay source. The decoded
/// rows borrow the batch buffers and live in ``storage`` (caller-owned, so
/// the storage must outlive the feed).
pub fn batchSource(
    gpa: std.mem.Allocator,
    storage: *std.ArrayList(MarketEvent),
    schema: *const arrow.ArrowSchema,
    array: *const arrow.ArrowArray,
    symbol: []const u8,
    expected_type: ?bus.EventType,
    session_day: ?i64,
) Error!EventSource {
    try arrow_batch.decodeMarketBatch(gpa, schema, array, storage);
    return EventSource.init(symbol, expected_type, session_day, storage.items);
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/data/test_replay.py (the
// same-timestamp merge ordering and the source validation errors); the
// session windowing and Arrow-batch vectors pin the documented contract.
// ---------------------------------------------------------------------------

const testing = std.testing;
const dec = market.dec;

const stamp_ns: i128 = 1_705_674_600_000_000_000;
const micro: i128 = 1_000;
const day_ns: i128 = 86_400 * std.time.ns_per_s;

fn replayQuote(event_id: []const u8, offset_us: i128, sequence: u64, symbol: []const u8) MarketEvent {
    return .{ .quote = .{
        .event_id = event_id,
        .timestamp_ns = stamp_ns + offset_us * micro,
        .sequence = sequence,
        .symbol = symbol,
        .bid_price = dec.fromInt(100),
        .bid_quantity = dec.one,
        .ask_price = dec.fromInt(101),
        .ask_quantity = dec.one,
    } };
}

fn replayTrade(event_id: []const u8, offset_us: i128, sequence: u64, symbol: []const u8) MarketEvent {
    return .{ .trade = .{
        .event_id = event_id,
        .timestamp_ns = stamp_ns + offset_us * micro,
        .sequence = sequence,
        .symbol = symbol,
        .price = dec.fromInt(100) + 500_000_000_000_000_000,
        .quantity = dec.one,
    } };
}

test "same timestamp merge uses sequence class priority then feed order" {
    var feed = ReplayFeed.init(testing.allocator);
    defer feed.deinit();
    // Stream sources in feed order: the trade stream first, the quote stream
    // second (Python dict insertion order in the golden vector).
    try feed.addSource(try EventSource.init("SPY", .trade, null, &[_]MarketEvent{
        replayTrade("trade-sequence", 0, 2, "SPY"),
        replayTrade("trade-tie", 0, 4, "SPY"),
    }));
    try feed.addSource(try EventSource.init("SPY", .quote, null, &[_]MarketEvent{
        replayQuote("quote-sequence", 0, 2, "SPY"),
        replayQuote("quote-first", 0, 4, "SPY"),
        replayQuote("quote-second", 0, 4, "SPY"),
    }));
    try feed.addSession(try Session.init(19_741, stamp_ns - day_ns, stamp_ns + day_ns));

    var merged: std.ArrayList(MarketEvent) = .empty;
    defer merged.deinit(testing.allocator);
    try feed.mergeAll(&merged);

    const expected = [_][]const u8{
        "quote-sequence",
        "trade-sequence",
        "quote-first",
        "quote-second",
        "trade-tie",
    };
    try testing.expectEqual(expected.len, merged.items.len);
    for (expected, merged.items) |id, event| {
        try testing.expectEqualStrings(id, event.eventId());
    }
}

test "source order must be monotonic and symbols must match" {
    var feed = ReplayFeed.init(testing.allocator);
    defer feed.deinit();
    try feed.addSource(try EventSource.init("SPY", null, null, &[_]MarketEvent{
        replayQuote("late", 1, 1, "SPY"),
        replayQuote("early", 0, 2, "SPY"),
    }));
    try feed.addSession(Session.utcDay(19_741));
    var merged: std.ArrayList(MarketEvent) = .empty;
    defer merged.deinit(testing.allocator);
    try testing.expectError(error.RetrogradeSource, feed.mergeAll(&merged));

    var mismatch = ReplayFeed.init(testing.allocator);
    defer mismatch.deinit();
    try mismatch.addSource(try EventSource.init("SPY", null, null, &[_]MarketEvent{
        replayQuote("wrong", 0, 1, "QQQ"),
    }));
    try mismatch.addSession(Session.utcDay(19_741));
    try testing.expectError(error.SymbolMismatch, mismatch.mergeAll(&merged));

    var typed = ReplayFeed.init(testing.allocator);
    defer typed.deinit();
    try typed.addSource(try EventSource.init("SPY", .trade, null, &[_]MarketEvent{
        replayQuote("mis-typed", 0, 1, "SPY"),
    }));
    try typed.addSession(Session.utcDay(19_741));
    try testing.expectError(error.EventTypeMismatch, typed.mergeAll(&merged));
}

test "sessions are half-open windows and cursors persist across them" {
    var feed = ReplayFeed.init(testing.allocator);
    defer feed.deinit();
    try feed.addSource(try EventSource.init("SPY", null, null, &[_]MarketEvent{
        replayQuote("before", -1, 1, "SPY"),
        replayQuote("open", 0, 2, "SPY"),
        replayQuote("inside", 30, 3, "SPY"),
        replayQuote("at-close", 60, 4, "SPY"),
        replayQuote("after", 120, 5, "SPY"),
    }));
    // One-minute session windows over the STAMP minute and the next. The
    // "before" event is consumed by the first window's open drain; events
    // exactly at a close are excluded from that window.
    try feed.addSession(try Session.init(19_741, stamp_ns, stamp_ns + 60 * micro));
    try feed.addSession(try Session.init(19_742, stamp_ns + 60 * micro, stamp_ns + 120 * micro));

    var merged: std.ArrayList(MarketEvent) = .empty;
    defer merged.deinit(testing.allocator);
    try feed.mergeAll(&merged);

    const expected = [_][]const u8{ "open", "inside", "at-close" };
    try testing.expectEqual(expected.len, merged.items.len);
    for (expected, merged.items) |id, event| {
        try testing.expectEqualStrings(id, event.eventId());
    }
}

test "equal keys merge in feed order" {
    var feed = ReplayFeed.init(testing.allocator);
    defer feed.deinit();
    try feed.addSource(try EventSource.init("SPY", .quote, null, &[_]MarketEvent{
        replayQuote("second-source", 0, 1, "SPY"),
    }));
    try feed.addSource(try EventSource.init("SPY", .quote, null, &[_]MarketEvent{
        replayQuote("first-source", 0, 1, "SPY"),
    }));
    try feed.addSession(Session.utcDay(19_741));

    var merged: std.ArrayList(MarketEvent) = .empty;
    defer merged.deinit(testing.allocator);
    try feed.mergeAll(&merged);

    try testing.expectEqual(@as(usize, 2), merged.items.len);
    try testing.expectEqualStrings("second-source", merged.items[0].eventId());
    try testing.expectEqualStrings("first-source", merged.items[1].eventId());
}

test "batch sources merge with stream sources through the arrow bridge" {
    const trade_columns = [_]arrow_batch.TestColumn{
        .{ .name = "event_id", .format = "u", .utf8_values = &.{ "trade-sequence", "trade-tie" } },
        .{ .name = "timestamp", .format = "tsu:UTC", .int_values = &.{ @intCast(@divTrunc(stamp_ns, 1_000)), @intCast(@divTrunc(stamp_ns, 1_000)) } },
        .{ .name = "sequence", .format = "l", .int_values = &.{ 2, 4 } },
        .{ .name = "symbol", .format = "u", .utf8_values = &.{ "SPY", "SPY" } },
        .{ .name = "price", .format = "d:38,18", .decimal_values = &.{ dec.fromInt(100) + 500_000_000_000_000_000, dec.fromInt(100) + 500_000_000_000_000_000 } },
        .{ .name = "quantity", .format = "d:38,18", .decimal_values = &.{ dec.one, dec.one } },
    };
    const quote_columns = [_]arrow_batch.TestColumn{
        .{ .name = "event_id", .format = "u", .utf8_values = &.{ "quote-sequence", "quote-first", "quote-second" } },
        .{ .name = "timestamp", .format = "tsu:UTC", .int_values = &.{ @intCast(@divTrunc(stamp_ns, 1_000)), @intCast(@divTrunc(stamp_ns, 1_000)), @intCast(@divTrunc(stamp_ns, 1_000)) } },
        .{ .name = "sequence", .format = "l", .int_values = &.{ 2, 4, 4 } },
        .{ .name = "symbol", .format = "u", .utf8_values = &.{ "SPY", "SPY", "SPY" } },
        .{ .name = "bid_price", .format = "d:38,18", .decimal_values = &.{ dec.fromInt(100), dec.fromInt(100), dec.fromInt(100) } },
        .{ .name = "bid_quantity", .format = "d:38,18", .decimal_values = &.{ dec.one, dec.one, dec.one } },
        .{ .name = "ask_price", .format = "d:38,18", .decimal_values = &.{ dec.fromInt(101), dec.fromInt(101), dec.fromInt(101) } },
        .{ .name = "ask_quantity", .format = "d:38,18", .decimal_values = &.{ dec.one, dec.one, dec.one } },
    };
    var trade_batch = try arrow_batch.TestBatch.build(testing.allocator, &trade_columns);
    defer trade_batch.deinit();
    var quote_batch = try arrow_batch.TestBatch.build(testing.allocator, &quote_columns);
    defer quote_batch.deinit();

    var feed = ReplayFeed.init(testing.allocator);
    defer feed.deinit();

    var trade_rows: std.ArrayList(MarketEvent) = .empty;
    defer trade_rows.deinit(testing.allocator);
    try feed.addSource(try batchSource(
        testing.allocator,
        &trade_rows,
        &trade_batch.schema,
        &trade_batch.array,
        "SPY",
        .trade,
        null,
    ));

    var quote_rows: std.ArrayList(MarketEvent) = .empty;
    defer quote_rows.deinit(testing.allocator);
    try feed.addSource(try batchSource(
        testing.allocator,
        &quote_rows,
        &quote_batch.schema,
        &quote_batch.array,
        "SPY",
        .quote,
        null,
    ));

    try feed.addSession(Session.utcDay(19_741));

    var merged: std.ArrayList(MarketEvent) = .empty;
    defer merged.deinit(testing.allocator);
    try feed.mergeAll(&merged);

    const expected = [_][]const u8{
        "quote-sequence",
        "trade-sequence",
        "quote-first",
        "quote-second",
        "trade-tie",
    };
    try testing.expectEqual(expected.len, merged.items.len);
    for (expected, merged.items) |id, event| {
        try testing.expectEqualStrings(id, event.eventId());
    }
}
