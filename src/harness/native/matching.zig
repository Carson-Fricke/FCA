//! Synchronous deterministic resting-order venue for top-of-book replay,
//! ported from ``src/harness/venues/sim.py`` (the equities single-leg path;
//! the options multi-leg engine stays Python-side).
//!
//! Commands return no outcomes. Acknowledgements, fills, cancellations,
//! expiries, and replacement reports are appended to the venue's ordered
//! record logs and scheduled onto the injected deterministic event bus
//! (``bus.zig``) with explicit timestamps, exactly like the Python venue.
//! The native bus envelope carries no payload, so the adapter routes market
//! payloads with a direct ``onMarket`` call at dispatch time; report and fill
//! envelopes are matched back to orders by ``event_id`` through the bus
//! subscription installed at construction.
//!
//! Determinism rules: response timestamp is the observed event head (optionally
//! reconciled with a ``SimClock``) plus the venue latency; sequences are
//! allocated in schedule order; matching iterates orders in submission order.

const std = @import("std");
const bus = @import("bus.zig");
const clock_mod = @import("clock.zig");
const fills = @import("fills.zig");
const market = @import("market.zig");
const oms = @import("oms.zig");

const dec = oms.dec;
const MarketEvent = market.MarketEvent;
const MarketOrder = market.MarketOrder;

pub const Error = bus.Error || error{
    InvalidSymbol,
    InvalidDecimal,
    OutOfRange,
    /// Command refers to an unknown simulated order (Python
    /// ``UnknownSimOrderError``).
    UnknownOrder,
    /// Order identifier submitted more than once (Python
    /// ``DuplicateSimOrderError``).
    DuplicateOrder,
    /// Command on an inactive order or a pending-response race (Python
    /// ``SimVenueError``).
    VenueState,
    /// Fill decision inconsistent with the working order.
    DecisionLedger,
    /// A timestamped order or market event is required before responses.
    NoTimestamp,
};

/// Immutable inspection snapshot of one venue-side order (Python
/// ``SimOrderState``).
pub const SimOrderState = struct {
    order: MarketOrder,
    status: oms.OrderStatus,
    filled_quantity: i128,
    remaining_quantity: i128,
    armed: bool,
    triggering_market: ?MarketEvent,
    decision_price: ?i128,
    last_decision: ?fills.FillDecision,

    pub fn orderId(self: *const SimOrderState) []const u8 {
        return self.order.order_id;
    }

    /// Whether the venue can still match or cancel the order.
    pub fn isActive(self: *const SimOrderState) bool {
        return !oms.isTerminalStatus(self.status);
    }

    /// Whether a stop order has crossed its trigger.
    pub fn triggered(self: *const SimOrderState) bool {
        if (self.order.order_type == .stop or self.order.order_type == .stop_limit) {
            return !self.armed;
        }
        return true;
    }
};

/// One scheduled execution report record (Python ``ExecutionReport``);
/// quantities are unscaled at scale 18.
pub const ReportRecord = struct {
    event_id: []const u8,
    report_id: []const u8,
    order_id: []const u8,
    timestamp_ns: i128,
    sequence: u64,
    status: oms.OrderStatus,
    execution_type: oms.ExecutionType,
    order_quantity: i128,
    filled_quantity: i128,
    remaining_quantity: i128,
    average_fill_price: ?i128,
    last_fill_quantity: i128 = 0,
    last_fill_price: ?i128 = null,
};

/// One scheduled fill record (Python ``Fill``).
pub const FillRecord = struct {
    event_id: []const u8,
    fill_id: []const u8,
    order_id: []const u8,
    symbol: []const u8,
    side: oms.Side,
    quantity: i128,
    price: i128,
    liquidity: fills.Liquidity,
    timestamp_ns: i128,
    sequence: u64,
};

const WorkingOrder = struct {
    order: MarketOrder,
    status: oms.OrderStatus = .accepted,
    filled_quantity: i128 = 0,
    filled_notional: i128 = 0,
    armed: bool = false,
    stop_triggered: bool = false,
    triggering_market: ?MarketEvent = null,
    decision_price: ?i128 = null,
    last_decision: ?fills.FillDecision = null,
    response_pending: bool = false,

    fn remainingQuantity(self: *const WorkingOrder) i128 {
        return self.order.quantity - self.filled_quantity;
    }

    fn isActive(self: *const WorkingOrder) bool {
        return !oms.isTerminalStatus(self.status);
    }
};

const PendingReport = struct {
    event_id: []const u8,
    order_id: []const u8,
    execution_type: oms.ExecutionType,
};

const LatestMarket = struct {
    symbol: []const u8,
    event: MarketEvent,
};

/// Match single-leg orders against explicit quote/trade events (Python
/// ``SimulatedVenue``). The default fill model is ``SimpleTouch``.
pub const SimulatedVenue = struct {
    gpa: std.mem.Allocator,
    event_bus: *bus.EventBus,
    fill_model: fills.FillModel,
    latency_ns: i128 = 0,
    venue_name: []const u8 = "sim",
    sim_clock: ?*clock_mod.SimClock = null,

    orders: std.ArrayList(WorkingOrder) = .empty,
    used_order_ids: std.ArrayList([]const u8) = .empty,
    sequence_head: i128 = -1,
    ordinal: u64 = 0,
    head_ns: ?i128 = null,
    latest_market: ?MarketEvent = null,
    latest_markets: std.ArrayList(LatestMarket) = .empty,
    decisions: std.ArrayList(fills.FillDecision) = .empty,
    report_records: std.ArrayList(ReportRecord) = .empty,
    fill_records: std.ArrayList(FillRecord) = .empty,
    pending_reports: std.ArrayList(PendingReport) = .empty,
    owned_ids: std.ArrayList([]u8) = .empty,

    /// Construct the venue in place and install its bus subscriptions. The
    /// venue must outlive the bus: the subscriptions borrow ``self``.
    pub fn init(
        gpa: std.mem.Allocator,
        event_bus: *bus.EventBus,
        fill_model: fills.FillModel,
        latency_ns: i128,
        sim_clock: ?*clock_mod.SimClock,
        self: *SimulatedVenue,
    ) Error!void {
        if (latency_ns < 0) return error.InvalidArgument;
        self.* = .{
            .gpa = gpa,
            .event_bus = event_bus,
            .fill_model = fill_model,
            .latency_ns = latency_ns,
            .sim_clock = sim_clock,
        };
        try event_bus.subscribe(.{ .event_type = .execution_report }, onReportEnvelope, self);
        try event_bus.subscribe(.{ .event_type = .fill }, onFillEnvelope, self);
    }

    pub fn deinit(self: *SimulatedVenue) void {
        self.orders.deinit(self.gpa);
        self.used_order_ids.deinit(self.gpa);
        self.latest_markets.deinit(self.gpa);
        self.decisions.deinit(self.gpa);
        self.report_records.deinit(self.gpa);
        self.fill_records.deinit(self.gpa);
        self.pending_reports.deinit(self.gpa);
        for (self.owned_ids.items) |id| self.gpa.free(id);
        self.owned_ids.deinit(self.gpa);
    }

    /// Accept an order and schedule its acknowledgement and outcome.
    pub fn submit(self: *SimulatedVenue, order: MarketOrder) Error!void {
        try order.validate();
        if (self.findUsedOrderId(order.order_id) != null) return error.DuplicateOrder;
        self.observe(order.timestamp_ns, order.sequence);
        try self.orders.append(self.gpa, .{
            .order = order,
            .armed = order.order_type == .stop or order.order_type == .stop_limit,
        });
        try self.used_order_ids.append(self.gpa, order.order_id);
        const working = &self.orders.items[self.orders.items.len - 1];
        try self.scheduleReport(working, .acknowledged, .accepted, 0, null);
        const latest = self.latestMarketFor(order.symbol);
        if (latest != null and !working.armed) {
            const decision = try self.attempt(working, latest.?, null);
            if (decision == null and
                order.time_in_force == .ioc and
                try self.isMatchingOpportunity(working, latest.?))
            {
                try self.scheduleExpiry(working, false);
            }
        }
    }

    /// Schedule cancellation confirmation for an active non-pending order.
    pub fn cancel(self: *SimulatedVenue, order_id: []const u8) Error!void {
        const working = try self.workingOrder(order_id);
        if (!working.isActive()) return error.VenueState;
        if (working.response_pending) return error.VenueState;
        working.status = .canceled;
        working.response_pending = true;
        try self.scheduleReport(working, .canceled, .canceled, 0, null);
    }

    /// Atomically report replacement and begin the replacement order.
    pub fn replace(self: *SimulatedVenue, order_id: []const u8, replacement: MarketOrder) Error!void {
        try replacement.validate();
        const working = try self.workingOrder(order_id);
        if (!working.isActive()) return error.VenueState;
        if (working.response_pending) return error.VenueState;
        if (self.findUsedOrderId(replacement.order_id) != null) return error.DuplicateOrder;
        if (!std.mem.eql(u8, replacement.symbol, working.order.symbol)) return error.VenueState;
        if (replacement.side != working.order.side) return error.VenueState;

        working.status = .replaced;
        working.response_pending = true;
        try self.used_order_ids.append(self.gpa, replacement.order_id);
        try self.scheduleReport(working, .replaced, .replaced, 0, null);

        try self.orders.append(self.gpa, .{
            .order = replacement,
            .armed = replacement.order_type == .stop or replacement.order_type == .stop_limit,
        });
        const replacement_working = &self.orders.items[self.orders.items.len - 1];
        try self.scheduleReport(replacement_working, .acknowledged, .accepted, 0, null);
        const latest = self.latestMarketFor(replacement.symbol);
        if (latest != null and !replacement_working.armed) {
            const decision = try self.attempt(replacement_working, latest.?, null);
            if (decision == null and
                replacement.time_in_force == .ioc and
                try self.isMatchingOpportunity(replacement_working, latest.?))
            {
                try self.scheduleExpiry(replacement_working, false);
            }
        }
    }

    /// Validate the protocol command; positions belong to the accounting hub.
    pub fn requestPositionSnapshot(self: *const SimulatedVenue, account_id: []const u8) Error!void {
        _ = self;
        if (account_id.len == 0) return error.InvalidArgument;
    }

    /// Return an immutable venue-side state snapshot.
    pub fn state(self: *const SimulatedVenue, order_id: []const u8) Error!SimOrderState {
        const working = try self.workingOrderConst(order_id);
        return self.snapshot(working);
    }

    /// Return fill decisions in deterministic matching order.
    pub fn decisionsSlice(self: *const SimulatedVenue) []const fills.FillDecision {
        return self.decisions.items;
    }

    /// Return all decisions made for one order.
    pub fn decisionsFor(self: *const SimulatedVenue, order_id: []const u8) Error!usize {
        _ = try self.workingOrder(order_id);
        var count: usize = 0;
        for (self.decisions.items) |decision| {
            if (std.mem.eql(u8, decision.order_id, order_id)) count += 1;
        }
        return count;
    }

    /// Route one market payload to the venue. Called by the adapter at the
    /// event's dispatch time (the native bus envelope carries no payload).
    pub fn onMarket(self: *SimulatedVenue, event: MarketEvent) Error!void {
        try event.validate();
        self.observe(event.timestampNs(), event.sequence());
        self.latest_market = event;
        try self.upsertLatestMarket(event);
        var available_buy: i128 = switch (event) {
            .trade => |trade| trade.quantity,
            .quote => |quote| quote.ask_quantity,
        };
        var available_sell: i128 = switch (event) {
            .trade => |trade| trade.quantity,
            .quote => |quote| quote.bid_quantity,
        };
        for (self.orders.items) |*working| {
            if (!working.isActive() or working.response_pending) continue;
            if (!std.mem.eql(u8, working.order.symbol, event.symbol())) continue;
            const available: ?i128 = if (working.order.side == .buy) available_buy else available_sell;
            const decision = try self.attempt(working, event, available);
            if (decision == null and try self.isMatchingOpportunity(working, event)) {
                if (working.order.time_in_force == .ioc) {
                    try self.scheduleExpiry(working, false);
                }
            } else if (decision != null) {
                const taken = decision.?.quantity;
                if (working.order.side == .buy) available_buy -= taken else available_sell -= taken;
            }
        }
    }

    fn attempt(
        self: *SimulatedVenue,
        working: *WorkingOrder,
        snap: MarketEvent,
        available_quantity: ?i128,
    ) Error!?fills.FillDecision {
        if (working.remainingQuantity() <= 0) return null;
        const is_stop = working.order.order_type == .stop or working.order.order_type == .stop_limit;
        if (is_stop and !working.stop_triggered) {
            if (!try self.isTriggered(working.order, snap)) return null;
            working.stop_triggered = true;
            working.armed = false;
            working.triggering_market = snap;
        }
        var decision = (try self.fill_model.decide(working.order, snap, .{
            .remaining_quantity = working.remainingQuantity(),
            .available_quantity = available_quantity,
            .stop_triggered = working.stop_triggered,
        })) orelse return null;
        if (is_stop and working.triggering_market != null and
            !sameMarket(decision.triggering_market, working.triggering_market.?))
        {
            decision.triggering_market = working.triggering_market.?;
        }
        working.triggering_market = decision.triggering_market;
        working.decision_price = decision.decision_price;
        working.last_decision = decision;
        try self.decisions.append(self.gpa, decision);
        try self.scheduleFill(working, decision);
        return decision;
    }

    fn scheduleFill(self: *SimulatedVenue, working: *WorkingOrder, decision: fills.FillDecision) Error!void {
        if (!std.mem.eql(u8, decision.order_id, working.order.order_id)) return error.DecisionLedger;
        if (decision.quantity > working.remainingQuantity()) return error.DecisionLedger;
        working.filled_quantity = try dec.add(working.filled_quantity, decision.quantity);
        working.filled_notional = try dec.add(
            working.filled_notional,
            try dec.mul(decision.quantity, decision.price),
        );
        working.status = if (working.remainingQuantity() == 0)
            .filled
        else
            .partially_filled;
        working.response_pending = true;
        const status = working.status;
        const execution_type: oms.ExecutionType = if (status == .filled) .fill else .partial_fill;
        try self.scheduleReport(working, execution_type, status, decision.quantity, decision.price);

        const timestamp = try self.responseTimestamp();
        const fill_id = try self.nextId("fill");
        const sequence = self.nextSequence();
        try self.fill_records.append(self.gpa, .{
            .event_id = fill_id,
            .fill_id = fill_id,
            .order_id = working.order.order_id,
            .symbol = working.order.symbol,
            .side = working.order.side,
            .quantity = decision.quantity,
            .price = decision.price,
            .liquidity = decision.liquidity,
            .timestamp_ns = timestamp,
            .sequence = sequence,
        });
        try self.pending_reports.append(self.gpa, .{
            .event_id = fill_id,
            .order_id = working.order.order_id,
            .execution_type = .fill,
        });
        try self.event_bus.schedule(bus.Event.initDefault(.fill, timestamp, sequence, fill_id));
        if (status == .partially_filled and working.order.time_in_force == .ioc) {
            try self.scheduleExpiry(working, true);
        }
    }

    fn scheduleExpiry(self: *SimulatedVenue, working: *WorkingOrder, allow_pending: bool) Error!void {
        if (!working.isActive() or (working.response_pending and !allow_pending)) return;
        working.status = .expired;
        working.response_pending = true;
        try self.scheduleReport(working, .expired, .expired, 0, null);
    }

    fn scheduleReport(
        self: *SimulatedVenue,
        working: *WorkingOrder,
        execution_type: oms.ExecutionType,
        status: oms.OrderStatus,
        last_fill_quantity: i128,
        last_fill_price: ?i128,
    ) Error!void {
        const timestamp = try self.responseTimestamp();
        const report_id = try self.nextId("report");
        const average: ?i128 = if (working.filled_quantity == 0)
            null
        else
            try dec.div(working.filled_notional, working.filled_quantity);
        const sequence = self.nextSequence();
        try self.report_records.append(self.gpa, .{
            .event_id = report_id,
            .report_id = report_id,
            .order_id = working.order.order_id,
            .timestamp_ns = timestamp,
            .sequence = sequence,
            .status = status,
            .execution_type = execution_type,
            .order_quantity = working.order.quantity,
            .filled_quantity = working.filled_quantity,
            .remaining_quantity = working.remainingQuantity(),
            .average_fill_price = average,
            .last_fill_quantity = last_fill_quantity,
            .last_fill_price = last_fill_price,
        });
        try self.pending_reports.append(self.gpa, .{
            .event_id = report_id,
            .order_id = working.order.order_id,
            .execution_type = execution_type,
        });
        try self.event_bus.schedule(bus.Event.initDefault(.execution_report, timestamp, sequence, report_id));
    }

    fn isMatchingOpportunity(self: *SimulatedVenue, working: *const WorkingOrder, snap: MarketEvent) Error!bool {
        if (working.order.order_type == .stop or working.order.order_type == .stop_limit) {
            return working.stop_triggered or try self.isTriggered(working.order, snap);
        }
        return true;
    }

    fn isTriggered(self: *SimulatedVenue, order: MarketOrder, snap: MarketEvent) Error!bool {
        if (self.fill_model.isSimpleTouch()) {
            return fills.SimpleTouch.isTriggered(order, snap);
        }
        const stop_price = order.stop_price orelse return false;
        const price: i128 = switch (snap) {
            .trade => |trade| trade.price,
            .quote => |quote| if (order.side == .buy) quote.ask_price else quote.bid_price,
        };
        return if (order.side == .buy) price >= stop_price else price <= stop_price;
    }

    fn snapshot(self: *const SimulatedVenue, working: *const WorkingOrder) SimOrderState {
        _ = self;
        return .{
            .order = working.order,
            .status = working.status,
            .filled_quantity = working.filled_quantity,
            .remaining_quantity = working.remainingQuantity(),
            .armed = working.armed,
            .triggering_market = working.triggering_market,
            .decision_price = working.decision_price,
            .last_decision = working.last_decision,
        };
    }

    /// Return active order snapshots in submission order. The caller owns
    /// ``out``.
    pub fn activeOrders(self: *const SimulatedVenue, out: *std.ArrayList(SimOrderState)) Error!void {
        for (self.orders.items) |*working| {
            if (working.isActive()) try out.append(self.gpa, self.snapshot(working));
        }
    }

    fn upsertLatestMarket(self: *SimulatedVenue, event: MarketEvent) Error!void {
        for (self.latest_markets.items) |*entry| {
            if (std.mem.eql(u8, entry.symbol, event.symbol())) {
                entry.event = event;
                return;
            }
        }
        try self.latest_markets.append(self.gpa, .{ .symbol = event.symbol(), .event = event });
    }

    fn latestMarketFor(self: *const SimulatedVenue, symbol: []const u8) ?MarketEvent {
        for (self.latest_markets.items) |entry| {
            if (std.mem.eql(u8, entry.symbol, symbol)) return entry.event;
        }
        return null;
    }

    fn findUsedOrderId(self: *const SimulatedVenue, order_id: []const u8) ?usize {
        for (self.used_order_ids.items, 0..) |used, index| {
            if (std.mem.eql(u8, used, order_id)) return index;
        }
        return null;
    }

    fn workingOrder(self: *SimulatedVenue, order_id: []const u8) Error!*WorkingOrder {
        for (self.orders.items) |*entry| {
            if (std.mem.eql(u8, entry.order.order_id, order_id)) return entry;
        }
        return error.UnknownOrder;
    }

    fn workingOrderConst(self: *const SimulatedVenue, order_id: []const u8) Error!*const WorkingOrder {
        for (self.orders.items) |*entry| {
            if (std.mem.eql(u8, entry.order.order_id, order_id)) return entry;
        }
        return error.UnknownOrder;
    }

    fn observe(self: *SimulatedVenue, timestamp_ns: i128, sequence: u64) void {
        if (self.head_ns == null or timestamp_ns > self.head_ns.?) self.head_ns = timestamp_ns;
        const sequence_wide: i128 = sequence;
        self.sequence_head = @max(self.sequence_head, sequence_wide);
    }

    fn observeEnvelope(self: *SimulatedVenue, event: *const bus.Event) void {
        self.observe(event.timestamp_ns, event.sequence);
    }

    fn responseTimestamp(self: *SimulatedVenue) Error!i128 {
        var head = self.head_ns;
        if (self.sim_clock) |sim| {
            const clock_head = sim.now();
            head = if (head == null) clock_head else @max(head.?, clock_head);
        }
        const base = head orelse return error.NoTimestamp;
        return base + self.latency_ns;
    }

    fn nextSequence(self: *SimulatedVenue) u64 {
        self.sequence_head += 1;
        return @intCast(self.sequence_head);
    }

    fn nextId(self: *SimulatedVenue, kind: []const u8) Error![]const u8 {
        self.ordinal += 1;
        const id = try std.fmt.allocPrint(self.gpa, "{s}-{s}-{d}", .{ self.venue_name, kind, self.ordinal });
        try self.owned_ids.append(self.gpa, id);
        return id;
    }

    fn onReportEnvelope(context: ?*anyopaque, event: *const bus.Event) void {
        const self: *SimulatedVenue = @ptrCast(@alignCast(context orelse return));
        for (self.pending_reports.items) |pending| {
            if (!std.mem.eql(u8, pending.event_id, event.event_id)) continue;
            if (pending.execution_type == .acknowledged) return;
            for (self.orders.items) |*entry| {
                if (std.mem.eql(u8, entry.order.order_id, pending.order_id)) {
                    entry.response_pending = false;
                    return;
                }
            }
            return;
        }
    }

    fn onFillEnvelope(context: ?*anyopaque, event: *const bus.Event) void {
        const self: *SimulatedVenue = @ptrCast(@alignCast(context orelse return));
        self.observeEnvelope(event);
    }
};

fn sameMarket(a: MarketEvent, b: MarketEvent) bool {
    return a.timestampNs() == b.timestampNs() and
        a.sequence() == b.sequence() and
        std.mem.eql(u8, a.eventId(), b.eventId());
}
