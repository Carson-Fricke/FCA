//! Single-threaded deterministic event queue and synchronous event bus,
//! ported from ``src/harness/core/events.py`` (with the scheduler-facing
//! semantics of ``src/harness/core/scheduler.py`` pinned by tests).
//!
//! Ordering contract (mirrored exactly from the Python engine): events are
//! dispatched by timestamp, then source sequence, then the fixed serialized
//! event-class priority table ``EVENT_CLASS_PRIORITY``, then incoming feed
//! order. The queue has no locking; one caller owns it from one event loop.
//!
//! The dispatch digest is byte-identical to Python's ``EventBus``: SHA-256
//! over, per dispatched event, the big-endian u64 identity length followed by
//! ``"<python type name>\0<event_type value>\0<isoformat microseconds>\0<sequence>\0<event_id>"``.

const std = @import("std");
const clock = @import("clock.zig");

pub const Error = error{
    OutOfMemory,
    /// Event or envelope field is empty/malformed.
    InvalidArgument,
    /// Event timestamp is older than the clock head (or, for a queue without
    /// an advanceable clock, older than the last dispatch).
    StaleTimestamp,
    /// Event ordering key precedes the last dispatched key.
    RetrogradeOrderKey,
    /// Dispatch identity exceeded the fixed digest buffer.
    IdentityTooLong,
} || clock.SimClockError;

/// Serialized event classes, mirroring ``harness.core.types.events.EventType``.
pub const EventType = enum(u8) {
    quote,
    trade,
    order,
    multi_leg_order,
    execution_report,
    fill,
    position,
    book,
    account,

    /// Serialized value used in the dispatch identity (``EventType.value``).
    pub fn value(self: EventType) []const u8 {
        return switch (self) {
            .quote => "quote",
            .trade => "trade",
            .order => "order",
            .multi_leg_order => "multi_leg_order",
            .execution_report => "execution_report",
            .fill => "fill",
            .position => "position",
            .book => "book",
            .account => "account",
        };
    }
};

/// The public ordering contract, mirrored insertion-for-insertion from
/// ``harness.core.events.EVENT_CLASS_PRIORITY``. Indexed by ``EventType``.
/// Insertion order and class names never affect priority.
pub const EVENT_CLASS_PRIORITY = [9]u8{
    0, // quote
    1, // trade
    2, // order
    3, // multi_leg_order
    4, // execution_report
    5, // fill
    6, // position
    7, // book
    8, // account
};

pub fn classPriority(event_type: EventType) u8 {
    return EVENT_CLASS_PRIORITY[@intFromEnum(event_type)];
}

/// Default Python-qualified class name used in the dispatch identity for core
/// event records. Scheduler records (``SessionEvent`` and
/// ``ScheduledCallbackEvent``, both ``EventType.order``) must override this
/// with ``harness.core.scheduler.SessionEvent`` /
/// ``harness.core.scheduler.ScheduledCallbackEvent``.
pub fn defaultTypeName(event_type: EventType) []const u8 {
    return switch (event_type) {
        .quote => "harness.core.types.events.QuoteEvent",
        .trade => "harness.core.types.events.TradeEvent",
        .order => "harness.core.types.orders.SingleLegOrder",
        .multi_leg_order => "harness.core.types.orders.MultiLegOrder",
        .execution_report => "harness.core.types.accounting.ExecutionReport",
        .fill => "harness.core.types.accounting.Fill",
        .position => "harness.core.types.accounting.Position",
        .book => "harness.core.types.accounting.Book",
        .account => "harness.core.types.accounting.AccountSnapshot",
    };
}

/// One event crossing the deterministic queue. ``type_name`` is the
/// Python-qualified record class name that enters the dispatch identity; the
/// remaining fields mirror ``harness.core.types.events.Event``.
pub const Event = struct {
    type_name: []const u8,
    event_type: EventType,
    timestamp_ns: i128,
    sequence: u64,
    event_id: []const u8,

    pub fn init(
        type_name: []const u8,
        event_type: EventType,
        timestamp_ns: i128,
        sequence: u64,
        event_id: []const u8,
    ) Event {
        return .{
            .type_name = type_name,
            .event_type = event_type,
            .timestamp_ns = timestamp_ns,
            .sequence = sequence,
            .event_id = event_id,
        };
    }

    pub fn initDefault(
        event_type: EventType,
        timestamp_ns: i128,
        sequence: u64,
        event_id: []const u8,
    ) Event {
        return init(defaultTypeName(event_type), event_type, timestamp_ns, sequence, event_id);
    }
};

pub const DispatchKey = struct {
    timestamp_ns: i128,
    sequence: u64,
    class_priority: u8,
};

fn dispatchKeyLess(a: DispatchKey, b: DispatchKey) bool {
    if (a.timestamp_ns != b.timestamp_ns) return a.timestamp_ns < b.timestamp_ns;
    if (a.sequence != b.sequence) return a.sequence < b.sequence;
    return a.class_priority < b.class_priority;
}

const QueuedEvent = struct {
    timestamp_ns: i128,
    sequence: u64,
    class_priority: u8,
    feed_ordinal: u64,
    event: Event,
};

fn queuedLess(a: QueuedEvent, b: QueuedEvent) bool {
    if (a.timestamp_ns != b.timestamp_ns) return a.timestamp_ns < b.timestamp_ns;
    if (a.sequence != b.sequence) return a.sequence < b.sequence;
    if (a.class_priority != b.class_priority) return a.class_priority < b.class_priority;
    return a.feed_ordinal < b.feed_ordinal;
}

fn heapPush(gpa: std.mem.Allocator, heap: *std.ArrayList(QueuedEvent), item: QueuedEvent) Error!void {
    try heap.append(gpa, item);
    const items = heap.items;
    var child = items.len - 1;
    while (child > 0) {
        const parent = (child - 1) / 2;
        if (!queuedLess(items[child], items[parent])) break;
        std.mem.swap(QueuedEvent, &items[child], &items[parent]);
        child = parent;
    }
}

fn siftDown(items: []QueuedEvent, root: usize) void {
    var index = root;
    while (true) {
        const left = 2 * index + 1;
        const right = 2 * index + 2;
        var smallest = index;
        if (left < items.len and queuedLess(items[left], items[smallest])) smallest = left;
        if (right < items.len and queuedLess(items[right], items[smallest])) smallest = right;
        if (smallest == index) return;
        std.mem.swap(QueuedEvent, &items[index], &items[smallest]);
        index = smallest;
    }
}

/// Priority queue ordered by time, source sequence, class, then feed order.
/// The queue intentionally has no locking. The clock is optional: a non-null
/// clock must be a ``SimClock`` (the only advanceable clock in the native
/// core), which the queue advances to each dispatched timestamp; a null clock
/// models the non-advanceable Python-side clock boundary (wall time never
/// enters the native core), where only the last-dispatch minimum applies.
pub const EventQueue = struct {
    gpa: std.mem.Allocator,
    clock: ?*clock.SimClock,
    heap: std.ArrayList(QueuedEvent) = .empty,
    feed_ordinal: u64 = 0,
    last_dispatched_key: ?DispatchKey = null,

    pub fn init(gpa: std.mem.Allocator, sim_clock: ?*clock.SimClock) EventQueue {
        return .{ .gpa = gpa, .clock = sim_clock };
    }

    pub fn deinit(self: *EventQueue) void {
        self.heap.deinit(self.gpa);
    }

    pub fn count(self: *const EventQueue) usize {
        return self.heap.items.len;
    }

    /// Queue an event while preserving insertion order for exact ties.
    pub fn enqueue(self: *EventQueue, event: Event) Error!void {
        if (event.event_id.len == 0 or event.type_name.len == 0) return error.InvalidArgument;
        const priority = classPriority(event.event_type);
        const key = DispatchKey{
            .timestamp_ns = event.timestamp_ns,
            .sequence = event.sequence,
            .class_priority = priority,
        };
        var minimum: ?i128 = if (self.last_dispatched_key) |last| last.timestamp_ns else null;
        if (self.clock) |sim| minimum = sim.now();
        if (minimum) |bound| {
            if (event.timestamp_ns < bound) return error.StaleTimestamp;
        }
        if (self.last_dispatched_key) |last| {
            if (dispatchKeyLess(key, last)) return error.RetrogradeOrderKey;
        }
        try heapPush(self.gpa, &self.heap, .{
            .timestamp_ns = event.timestamp_ns,
            .sequence = event.sequence,
            .class_priority = priority,
            .feed_ordinal = self.feed_ordinal,
            .event = event,
        });
        self.feed_ordinal += 1;
    }

    /// Queue events in iteration order, which defines their feed order.
    pub fn enqueueMany(self: *EventQueue, events: []const Event) Error!void {
        for (events) |event| try self.enqueue(event);
    }

    /// Return the next event without removing it.
    pub fn peek(self: *const EventQueue) ?Event {
        if (self.heap.items.len == 0) return null;
        return self.heap.items[0].event;
    }

    /// Return the next dispatchable event, or null if none is queued. With a
    /// ``SimClock`` the head always advances to the dispatched timestamp; the
    /// Python wall-clock lateness gate stays a Python-side adapter concern.
    pub fn pop(self: *EventQueue) Error!?Event {
        if (self.heap.items.len == 0) return null;
        const queued = self.heap.items[0];
        if (self.clock) |sim| {
            _ = try sim.advanceTo(queued.timestamp_ns);
        }
        const last = self.heap.pop().?;
        if (self.heap.items.len > 0) {
            self.heap.items[0] = last;
            siftDown(self.heap.items, 0);
        }
        self.last_dispatched_key = .{
            .timestamp_ns = queued.timestamp_ns,
            .sequence = queued.sequence,
            .class_priority = queued.class_priority,
        };
        return queued.event;
    }
};

pub const Handler = *const fn (context: ?*anyopaque, event: *const Event) void;

/// Subscription selector. ``event_type`` mirrors Python ``EventType``
/// selectors; ``type_name`` mirrors Python event-class selectors (which match
/// concrete records such as scheduler events that share ``EventType.order``).
pub const Selector = union(enum) {
    any,
    event_type: EventType,
    type_name: []const u8,
};

const Subscription = struct {
    selector: Selector,
    handler: Handler,
    context: ?*anyopaque,

    fn matches(self: *const Subscription, event: *const Event) bool {
        return switch (self.selector) {
            .any => true,
            .event_type => |selected| event.event_type == selected,
            .type_name => |name| std.mem.eql(u8, event.type_name, name),
        };
    }
};

/// Dispatch queued events synchronously in one deterministic event loop.
pub const EventBus = struct {
    gpa: std.mem.Allocator,
    queue: EventQueue,
    subscriptions: std.ArrayList(Subscription) = .empty,
    dispatch_digest: std.crypto.hash.sha2.Sha256,
    dispatched_count: u64 = 0,

    pub fn init(gpa: std.mem.Allocator, sim_clock: ?*clock.SimClock) EventBus {
        return .{
            .gpa = gpa,
            .queue = EventQueue.init(gpa, sim_clock),
            .dispatch_digest = std.crypto.hash.sha2.Sha256.init(.{}),
        };
    }

    pub fn deinit(self: *EventBus) void {
        self.queue.deinit();
        self.subscriptions.deinit(self.gpa);
    }

    pub fn count(self: *const EventBus) usize {
        return self.queue.count();
    }

    pub fn dispatchedCount(self: *const EventBus) u64 {
        return self.dispatched_count;
    }

    /// Return a stable SHA-256 digest of event identities dispatched so far.
    pub fn dispatchHash(self: *const EventBus, out: *[64]u8) []const u8 {
        var copy = self.dispatch_digest;
        var digest: [32]u8 = undefined;
        copy.final(&digest);
        const hex = std.fmt.bytesToHex(digest, .lower);
        @memcpy(out, &hex);
        return out;
    }

    /// Register a handler; handlers run in registration order.
    pub fn subscribe(self: *EventBus, selector: Selector, handler: Handler, context: ?*anyopaque) Error!void {
        try self.subscriptions.append(self.gpa, .{
            .selector = selector,
            .handler = handler,
            .context = context,
        });
    }

    /// Publish one feed event to the deterministic queue.
    pub fn publish(self: *EventBus, event: Event) Error!void {
        try self.queue.enqueue(event);
    }

    /// Publish events in their incoming feed order.
    pub fn publishMany(self: *EventBus, events: []const Event) Error!void {
        try self.queue.enqueueMany(events);
    }

    /// Schedule a venue response at its explicit event timestamp.
    pub fn schedule(self: *EventBus, response: Event) Error!void {
        try self.queue.enqueue(response);
    }

    /// Dispatch one event and return it, or return null if none is queued.
    /// Subscribers are snapshotted at dispatch start, so handlers that append
    /// subscriptions (or schedule events) during dispatch never receive the
    /// in-flight event, mirroring the Python bus.
    pub fn dispatchNext(self: *EventBus) Error!?Event {
        const event = (try self.queue.pop()) orelse return null;
        const registered = self.subscriptions.items.len;
        var index: usize = 0;
        while (index < registered) : (index += 1) {
            const subscription = self.subscriptions.items[index];
            if (subscription.matches(&event)) subscription.handler(subscription.context, &event);
        }
        try self.recordDispatch(event);
        return event;
    }

    /// Dispatch queued events until empty or ``limit`` is reached; returns
    /// the number dispatched.
    pub fn drain(self: *EventBus, limit: ?usize) Error!usize {
        var dispatched: usize = 0;
        while (limit == null or dispatched < limit.?) {
            const event = try self.dispatchNext();
            if (event == null) break;
            dispatched += 1;
        }
        return dispatched;
    }

    fn recordDispatch(self: *EventBus, event: Event) Error!void {
        var buffer: [1024]u8 = undefined;
        var writer = IdentityWriter{ .buf = &buffer };
        try writer.writeAll(event.type_name);
        try writer.writeByte(0);
        try writer.writeAll(event.event_type.value());
        try writer.writeByte(0);
        var stamp_buffer: [40]u8 = undefined;
        try writer.writeAll(try isoformatMicros(event.timestamp_ns, &stamp_buffer));
        try writer.writeByte(0);
        try writer.writeUint(event.sequence);
        try writer.writeByte(0);
        try writer.writeAll(event.event_id);
        const identity = writer.slice();

        var length_bytes: [8]u8 = undefined;
        std.mem.writeInt(u64, &length_bytes, identity.len, .big);
        self.dispatch_digest.update(&length_bytes);
        self.dispatch_digest.update(identity);
        self.dispatched_count += 1;
    }
};

const IdentityWriter = struct {
    buf: []u8,
    index: usize = 0,

    fn slice(self: *const IdentityWriter) []const u8 {
        return self.buf[0..self.index];
    }

    fn writeByte(self: *IdentityWriter, byte: u8) Error!void {
        if (self.index >= self.buf.len) return error.IdentityTooLong;
        self.buf[self.index] = byte;
        self.index += 1;
    }

    fn writeByteN(self: *IdentityWriter, byte: u8, count: usize) Error!void {
        if (self.index + count > self.buf.len) return error.IdentityTooLong;
        @memset(self.buf[self.index .. self.index + count], byte);
        self.index += count;
    }

    fn writeAll(self: *IdentityWriter, bytes: []const u8) Error!void {
        if (self.index + bytes.len > self.buf.len) return error.IdentityTooLong;
        @memcpy(self.buf[self.index .. self.index + bytes.len], bytes);
        self.index += bytes.len;
    }

    fn writeUint(self: *IdentityWriter, value: u64) Error!void {
        var digits: [20]u8 = undefined;
        var length: usize = digits.len;
        var remainder = value;
        while (true) {
            length -= 1;
            digits[length] = @intCast('0' + @as(u8, @intCast(remainder % 10)));
            remainder /= 10;
            if (remainder == 0) break;
        }
        try self.writeAll(digits[length..]);
    }

    fn writeIntPadded(self: *IdentityWriter, value: u64, width: usize) Error!void {
        var digits: [20]u8 = undefined;
        var length: usize = digits.len;
        var remainder = value;
        while (true) {
            length -= 1;
            digits[length] = @intCast('0' + @as(u8, @intCast(remainder % 10)));
            remainder /= 10;
            if (remainder == 0) break;
        }
        const rendered = digits.len - length;
        if (rendered > width) return error.IdentityTooLong;
        try self.writeByteN('0', width - rendered);
        try self.writeAll(digits[length..]);
    }
};

/// Render an epoch-nanosecond instant exactly like Python's
/// ``datetime.isoformat(timespec="microseconds")`` on a UTC-aware datetime:
/// the six-digit fraction is always present and the offset is "+00:00".
fn isoformatMicros(timestamp_ns: i128, out: []u8) Error![]const u8 {
    const nanos_per_micro: i128 = 1_000;
    if (@mod(timestamp_ns, nanos_per_micro) != 0) return error.InvalidArgument;
    const micros: i128 = @divTrunc(timestamp_ns, nanos_per_micro);
    const micros_per_day: i128 = 86_400_000_000;
    const days: i128 = @divFloor(micros, micros_per_day);
    const micros_of_day: u64 = @intCast(micros - days * micros_per_day);
    const day_seconds: u32 = @intCast(micros_of_day / 1_000_000);
    const microsecond: u32 = @intCast(micros_of_day % 1_000_000);

    const civil = civilFromDays(days);
    if (civil.year < 1 or civil.year > 9999) return error.InvalidArgument;

    var writer = IdentityWriter{ .buf = out };
    try writer.writeIntPadded(@intCast(civil.year), 4);
    try writer.writeByte('-');
    try writer.writeIntPadded(civil.month, 2);
    try writer.writeByte('-');
    try writer.writeIntPadded(civil.day, 2);
    try writer.writeByte('T');
    try writer.writeIntPadded(day_seconds / 3600, 2);
    try writer.writeByte(':');
    try writer.writeIntPadded((day_seconds % 3600) / 60, 2);
    try writer.writeByte(':');
    try writer.writeIntPadded(day_seconds % 60, 2);
    try writer.writeByte('.');
    try writer.writeIntPadded(microsecond, 6);
    try writer.writeAll("+00:00");
    return writer.slice();
}

const Civil = struct { year: i64, month: u32, day: u32 };

/// Howard Hinnant's civil-from-days algorithm (same as types.zig).
fn civilFromDays(days_since_epoch: i128) Civil {
    const z = days_since_epoch + 719_468;
    const era: i128 = @divFloor(z, 146_097);
    const doe: u64 = @intCast(z - era * 146_097);
    const yoe: u64 = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    const y: i128 = @as(i128, @intCast(yoe)) + era * 400;
    const doy: u64 = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const mp: u64 = (5 * doy + 2) / 153;
    const d: u64 = doy - (153 * mp + 2) / 5 + 1;
    const m: u64 = if (mp < 10) mp + 3 else mp - 9;
    const year: i64 = @intCast(y + @as(i128, if (m <= 2) 1 else 0));
    return .{ .year = year, .month = @intCast(m), .day = @intCast(d) };
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/core/test_events.py and the
// ordering semantics of tests/harness/core/test_scheduler.py.
// ---------------------------------------------------------------------------

const testing = std.testing;

/// 2024-01-19T14:30:00Z in epoch nanoseconds.
const stamp_ns: i128 = 1_705_674_600_000_000_000;
const micro: i128 = 1_000;

test "event class priority mirrors the Python table exactly" {
    try testing.expectEqual(@as(u8, 0), classPriority(.quote));
    try testing.expectEqual(@as(u8, 1), classPriority(.trade));
    try testing.expectEqual(@as(u8, 2), classPriority(.order));
    try testing.expectEqual(@as(u8, 3), classPriority(.multi_leg_order));
    try testing.expectEqual(@as(u8, 4), classPriority(.execution_report));
    try testing.expectEqual(@as(u8, 5), classPriority(.fill));
    try testing.expectEqual(@as(u8, 6), classPriority(.position));
    try testing.expectEqual(@as(u8, 7), classPriority(.book));
    try testing.expectEqual(@as(u8, 8), classPriority(.account));
}

test "isoformat microseconds matches Python datetime.isoformat" {
    var buffer: [40]u8 = undefined;
    const whole = try isoformatMicros(stamp_ns, &buffer);
    try testing.expectEqualStrings("2024-01-19T14:30:00.000000+00:00", whole);
    const offset = try isoformatMicros(stamp_ns + 29 * micro, &buffer);
    try testing.expectEqualStrings("2024-01-19T14:30:00.000029+00:00", offset);
    try testing.expectError(error.InvalidArgument, isoformatMicros(stamp_ns + 1, &buffer));
}

test "queue orders by timestamp sequence class then feed order" {
    var sim = clock.SimClock.init(stamp_ns);
    var queue = EventQueue.init(testing.allocator, &sim);
    defer queue.deinit();

    const events = [_]Event{
        Event.initDefault(.fill, stamp_ns + 1 * micro, 4, "fill-priority"),
        Event.initDefault(.trade, stamp_ns + 1 * micro, 4, "trade-priority"),
        Event.initDefault(.quote, stamp_ns + 1 * micro, 4, "quote-first"),
        Event.initDefault(.quote, stamp_ns + 1 * micro, 4, "quote-second"),
        Event.initDefault(.quote, stamp_ns + 1 * micro, 5, "later-sequence"),
        Event.initDefault(.fill, stamp_ns + 1 * micro, 3, "earlier-sequence"),
        Event.initDefault(.trade, stamp_ns + 0 * micro, 100, "earliest-time"),
    };
    try queue.enqueueMany(&events);
    try testing.expectEqual(@as(usize, 7), queue.count());

    const expected = [_][]const u8{
        "earliest-time",
        "earlier-sequence",
        "quote-first",
        "quote-second",
        "trade-priority",
        "fill-priority",
        "later-sequence",
    };
    var dispatched: usize = 0;
    while (try queue.pop()) |event| : (dispatched += 1) {
        try testing.expect(dispatched < expected.len);
        try testing.expectEqualStrings(expected[dispatched], event.event_id);
    }
    try testing.expectEqual(expected.len, dispatched);
    try testing.expect(queue.peek() == null);
}

test "queue rejects events older than the sim clock head" {
    var sim = clock.SimClock.init(stamp_ns);
    var queue = EventQueue.init(testing.allocator, &sim);
    defer queue.deinit();

    try testing.expectError(
        error.StaleTimestamp,
        queue.enqueue(Event.initDefault(.quote, stamp_ns - 1 * micro, 0, "stale")),
    );

    try queue.enqueue(Event.initDefault(.quote, stamp_ns, 0, "current"));
    try testing.expect((try queue.pop()) != null);
    try testing.expectEqual(stamp_ns, sim.now());

    _ = try sim.advanceBy(2 * micro);
    try testing.expectError(
        error.StaleTimestamp,
        queue.enqueue(Event.initDefault(.quote, stamp_ns + 1 * micro, 1, "now-stale")),
    );
}

const VenueSink = struct {
    gpa: std.mem.Allocator,
    bus: *EventBus,
    handled: std.ArrayList([]const u8) = .empty,

    fn onQuote(context: ?*anyopaque, event: *const Event) void {
        const self: *VenueSink = @ptrCast(@alignCast(context.?));
        self.handled.append(self.gpa, event.event_id) catch @panic("test append failed");
        self.bus.schedule(Event.initDefault(
            .fill,
            event.timestamp_ns + 250 * micro,
            event.sequence,
            "venue-fill",
        )) catch @panic("test schedule failed");
    }

    fn onFill(context: ?*anyopaque, event: *const Event) void {
        const self: *VenueSink = @ptrCast(@alignCast(context.?));
        self.handled.append(self.gpa, event.event_id) catch @panic("test append failed");
    }
};

test "handler can schedule a future venue response" {
    var sim = clock.SimClock.init(stamp_ns);
    var bus = EventBus.init(testing.allocator, &sim);
    defer bus.deinit();
    var sink = VenueSink{ .gpa = testing.allocator, .bus = &bus };
    defer sink.handled.deinit(testing.allocator);

    try bus.subscribe(.{ .event_type = .quote }, VenueSink.onQuote, &sink);
    try bus.subscribe(.{ .event_type = .fill }, VenueSink.onFill, &sink);
    try bus.publish(Event.initDefault(.quote, stamp_ns, 7, "market-quote"));

    try testing.expectEqual(@as(usize, 2), try bus.drain(null));
    try testing.expectEqual(@as(u64, 2), bus.dispatchedCount());
    try testing.expectEqualStrings("market-quote", sink.handled.items[0]);
    try testing.expectEqualStrings("venue-fill", sink.handled.items[1]);
    try testing.expectEqual(stamp_ns + 250 * micro, sim.now());
}

test "scheduled response cannot precede the last dispatch key" {
    var sim = clock.SimClock.init(stamp_ns);
    var bus = EventBus.init(testing.allocator, &sim);
    defer bus.deinit();

    try bus.publish(Event.initDefault(.trade, stamp_ns, 7, "market-trade"));
    try testing.expect((try bus.dispatchNext()) != null);

    try testing.expectError(
        error.RetrogradeOrderKey,
        bus.schedule(Event.initDefault(.quote, stamp_ns, 7, "retrograde-response")),
    );
}

test "dispatch digest is byte-identical to the Python bus" {
    var sim = clock.SimClock.init(stamp_ns);
    var bus = EventBus.init(testing.allocator, &sim);
    defer bus.deinit();

    // The queue stores borrowed event slices, so ids must outlive dispatch.
    var ids: std.ArrayList([]u8) = .empty;
    defer {
        for (ids.items) |id| testing.allocator.free(id);
        ids.deinit(testing.allocator);
    }

    var index: usize = 0;
    while (index < 10_000) : (index += 1) {
        const offset_us: i128 = @intCast(index % 29);
        const sequence: u64 = @intCast(index % 17);
        const id = try std.fmt.allocPrint(testing.allocator, "event-{d}", .{index});
        try ids.append(testing.allocator, id);
        const event = switch (index % 3) {
            0 => Event.initDefault(.quote, stamp_ns + offset_us * micro, sequence, id),
            1 => Event.initDefault(.trade, stamp_ns + offset_us * micro, sequence, id),
            else => Event.initDefault(.fill, stamp_ns + offset_us * micro, sequence, id),
        };
        try bus.publish(event);
    }

    try testing.expectEqual(@as(usize, 10_000), try bus.drain(null));
    try testing.expectEqual(@as(u64, 10_000), bus.dispatchedCount());

    var hash_buffer: [64]u8 = undefined;
    const hash = bus.dispatchHash(&hash_buffer);
    try testing.expectEqualStrings(
        "949b0dd3fbf95bcb1b93baec06c20e8e5c66ad74e342df01b1e21d9eff769df2",
        hash,
    );
}

const SchedulerSink = struct {
    gpa: std.mem.Allocator,
    bus: *EventBus,
    seen: std.ArrayList([]const u8) = .empty,
    timestamps: std.ArrayList(i128) = .empty,
    recurring_remaining: usize = 2,

    fn onScheduled(context: ?*anyopaque, event: *const Event) void {
        const self: *SchedulerSink = @ptrCast(@alignCast(context.?));
        self.seen.append(self.gpa, event.event_id) catch @panic("test append failed");
        self.timestamps.append(self.gpa, event.timestamp_ns) catch @panic("test append failed");
        if (std.mem.startsWith(u8, event.event_id, "callback-recurring") and self.recurring_remaining > 0) {
            self.recurring_remaining -= 1;
            self.bus.schedule(.{
                .type_name = "harness.core.scheduler.ScheduledCallbackEvent",
                .event_type = .order,
                .timestamp_ns = event.timestamp_ns + std.time.ns_per_s,
                .sequence = event.sequence + 10,
                .event_id = event.event_id,
            }) catch @panic("test schedule failed");
        }
    }
};

test "absolute and recurring callbacks are synchronous and deterministic" {
    var sim = clock.SimClock.init(stamp_ns);
    var bus = EventBus.init(testing.allocator, &sim);
    defer bus.deinit();
    var sink = SchedulerSink{ .gpa = testing.allocator, .bus = &bus };
    defer {
        sink.seen.deinit(testing.allocator);
        sink.timestamps.deinit(testing.allocator);
    }
    try bus.subscribe(
        .{ .type_name = "harness.core.scheduler.ScheduledCallbackEvent" },
        SchedulerSink.onScheduled,
        &sink,
    );

    try bus.schedule(.{
        .type_name = "harness.core.scheduler.ScheduledCallbackEvent",
        .event_type = .order,
        .timestamp_ns = stamp_ns + 2 * std.time.ns_per_s,
        .sequence = 1,
        .event_id = "callback-absolute",
    });
    try bus.schedule(.{
        .type_name = "harness.core.scheduler.ScheduledCallbackEvent",
        .event_type = .order,
        .timestamp_ns = stamp_ns + 1 * std.time.ns_per_s,
        .sequence = 0,
        .event_id = "callback-recurring",
    });

    _ = try bus.drain(null);

    const expected_ids = [_][]const u8{
        "callback-recurring",
        "callback-absolute",
        "callback-recurring",
        "callback-recurring",
    };
    try testing.expectEqual(expected_ids.len, sink.seen.items.len);
    for (expected_ids, sink.seen.items) |expected, actual| {
        try testing.expectEqualStrings(expected, actual);
    }
    try testing.expectEqual(stamp_ns + 1 * std.time.ns_per_s, sink.timestamps.items[0]);
    try testing.expectEqual(stamp_ns + 2 * std.time.ns_per_s, sink.timestamps.items[1]);
    try testing.expectEqual(stamp_ns + 2 * std.time.ns_per_s, sink.timestamps.items[2]);
    try testing.expectEqual(stamp_ns + 3 * std.time.ns_per_s, sink.timestamps.items[3]);
    try testing.expectEqual(stamp_ns + 3 * std.time.ns_per_s, sim.now());
}

test "queue rejects events without identity" {
    var sim = clock.SimClock.init(stamp_ns);
    var queue = EventQueue.init(testing.allocator, &sim);
    defer queue.deinit();
    try testing.expectError(
        error.InvalidArgument,
        queue.enqueue(Event.initDefault(.quote, stamp_ns, 0, "")),
    );
}
