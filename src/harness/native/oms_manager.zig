//! Execution-report-driven order management registry, ported from
//! ``src/harness/oms/manager.py`` (the state machine lives in
//! ``oms_state.zig``).
//!
//! Machines stay in registration order, so active-state snapshots and the
//! replacement directive lists are deterministic exactly like the Python
//! registry. Every quantity is the pinned i128 fixed-point type (see
//! ``decimal.zig``).

const std = @import("std");
const state = @import("oms_state.zig");

pub const Error = state.Error;
pub const dec = @import("decimal.zig").dec;
pub const Side = state.Side;
pub const OrderType = state.OrderType;
pub const TimeInForce = state.TimeInForce;
pub const OrderStatus = state.OrderStatus;
pub const ExecutionType = state.ExecutionType;
pub const SingleLegOrder = state.SingleLegOrder;
pub const ExecutionReport = state.ExecutionReport;
pub const OrderSnapshot = state.OrderSnapshot;
pub const OrderStateMachine = state.OrderStateMachine;
pub const AverageArg = state.AverageArg;
pub const computed_average = state.computed_average;

/// Lifecycle of an atomic replacement request (Python ``ReplaceStatus``).
pub const ReplaceStatus = enum { pending, applied, aborted };

/// Action required for an active order at a session boundary.
pub const SessionAction = enum { expire, carry };

/// A reserved replacement and its report-confirmed outcome.
pub const ReplaceRecord = struct {
    original_order_id: []const u8,
    replacement: SingleLegOrder,
    status: ReplaceStatus = .pending,
    report_id: ?[]const u8 = null,

    pub fn replacementOrderId(self: *const ReplaceRecord) []const u8 {
        return self.replacement.order_id;
    }
};

/// A deterministic session-boundary policy decision.
pub const SessionDirective = struct {
    order_id: []const u8,
    action: SessionAction,
};

const MachineEntry = struct {
    machine: OrderStateMachine,
};

const ReplacementEntry = struct {
    original_order_id: []const u8,
    record: ReplaceRecord,
};

const LinkEntry = struct {
    from: []const u8,
    to: []const u8,
};

/// Track single-leg orders using execution reports as the mutation path.
pub const SingleLegOMS = struct {
    gpa: std.mem.Allocator,
    machines: std.ArrayList(MachineEntry) = .empty,
    used_order_ids: std.ArrayList([]const u8) = .empty,
    replacements: std.ArrayList(ReplacementEntry) = .empty,
    replaced_by_links: std.ArrayList(LinkEntry) = .empty,
    replaces_links: std.ArrayList(LinkEntry) = .empty,

    pub fn init(gpa: std.mem.Allocator) SingleLegOMS {
        return .{ .gpa = gpa };
    }

    pub fn deinit(self: *SingleLegOMS) void {
        for (self.machines.items) |*entry| entry.machine.deinit();
        self.machines.deinit(self.gpa);
        self.used_order_ids.deinit(self.gpa);
        self.replacements.deinit(self.gpa);
        self.replaced_by_links.deinit(self.gpa);
        self.replaces_links.deinit(self.gpa);
    }

    /// Register a validated local order in ``NEW`` state.
    pub fn submit(self: *SingleLegOMS, order: SingleLegOrder) Error!OrderSnapshot {
        if (order.order_id.len == 0 or order.symbol.len == 0) return error.InvalidArgument;
        if (order.quantity <= 0) return error.InvalidArgument;
        if (self.findUsedOrderId(order.order_id) != null) return error.DuplicateOrder;
        try self.used_order_ids.ensureUnusedCapacity(self.gpa, 1);
        try self.machines.ensureUnusedCapacity(self.gpa, 1);
        self.used_order_ids.appendAssumeCapacity(order.order_id);
        self.machines.appendAssumeCapacity(.{ .machine = OrderStateMachine.init(self.gpa, order) });
        return self.machines.items[self.machines.items.len - 1].machine.snapshot;
    }

    /// Route one report and atomically confirm any replacement.
    pub fn apply(self: *SingleLegOMS, report: ExecutionReport) Error!OrderSnapshot {
        const machine_index = try self.machineIndex(report.order_id);

        if (report.execution_type == .replaced) {
            const replacement_index = self.replacementIndex(report.order_id) orelse
                return error.ReplaceConflict;
            if (self.replacements.items[replacement_index].record.status != .pending) {
                return error.ReplaceConflict;
            }
            // Reserve capacity first so the registration writes after the old
            // order's terminal transition cannot fail, mirroring the Python
            // ordering guarantee under the synchronous kernel contract.
            try self.machines.ensureUnusedCapacity(self.gpa, 1);
            var replacement_machine = OrderStateMachine.init(
                self.gpa,
                self.replacements.items[replacement_index].record.replacement,
            );
            errdefer replacement_machine.deinit();
            const snapshot = try self.machines.items[machine_index].machine.apply(report);
            const replacement_id = self.replacements.items[replacement_index].record.replacement.order_id;
            try self.replaced_by_links.append(self.gpa, .{ .from = report.order_id, .to = replacement_id });
            try self.replaces_links.append(self.gpa, .{ .from = replacement_id, .to = report.order_id });
            self.replacements.items[replacement_index].record = .{
                .original_order_id = report.order_id,
                .replacement = self.replacements.items[replacement_index].record.replacement,
                .status = .applied,
                .report_id = report.report_id,
            };
            self.machines.appendAssumeCapacity(.{ .machine = replacement_machine });
            return snapshot;
        }

        const snapshot = try self.machines.items[machine_index].machine.apply(report);
        if (self.replacementIndex(report.order_id)) |replacement_index| {
            const record = &self.replacements.items[replacement_index].record;
            if (record.status == .pending and snapshot.isTerminal()) {
                self.replacements.items[replacement_index].record = .{
                    .original_order_id = report.order_id,
                    .replacement = record.replacement,
                    .status = .aborted,
                    .report_id = report.report_id,
                };
            }
        }
        return snapshot;
    }

    /// Reserve a replacement without changing either order's lifecycle. The
    /// replacement becomes visible as a ``NEW`` order only when a valid
    /// ``REPLACED`` execution report terminates the original order.
    pub fn requestReplace(self: *SingleLegOMS, original_order_id: []const u8, replacement_order: SingleLegOrder) Error!ReplaceRecord {
        const machine_index = try self.machineIndex(original_order_id);
        const original = self.machines.items[machine_index].machine.snapshot;
        if (original.status != .accepted and original.status != .partially_filled) {
            return error.ReplaceConflict;
        }
        if (self.replacementIndex(original_order_id) != null) return error.ReplaceConflict;
        if (self.findUsedOrderId(replacement_order.order_id) != null) return error.DuplicateOrder;
        if (!std.mem.eql(u8, replacement_order.symbol, original.order.symbol)) return error.ReplaceConflict;
        if (replacement_order.side != original.order.side) return error.ReplaceConflict;
        try self.used_order_ids.ensureUnusedCapacity(self.gpa, 1);
        const record = ReplaceRecord{
            .original_order_id = original_order_id,
            .replacement = replacement_order,
        };
        try self.replacements.append(self.gpa, .{
            .original_order_id = original_order_id,
            .record = record,
        });
        self.used_order_ids.appendAssumeCapacity(replacement_order.order_id);
        return record;
    }

    /// Return an order's current immutable state.
    pub fn state(self: *const SingleLegOMS, order_id: []const u8) Error!OrderSnapshot {
        const machine_index = try self.machineIndex(order_id);
        return self.machines.items[machine_index].machine.snapshot;
    }

    /// Return all accepted reports for an order in application order.
    pub fn reports(self: *const SingleLegOMS, order_id: []const u8) Error![]const ExecutionReport {
        const machine_index = try self.machineIndex(order_id);
        return self.machines.items[machine_index].machine.reports.items;
    }

    /// Return replacement tracking for an original order, if present.
    pub fn replacement(self: *const SingleLegOMS, original_order_id: []const u8) ?ReplaceRecord {
        const index = self.replacementIndex(original_order_id) orelse return null;
        return self.replacements.items[index].record;
    }

    /// Return the report-confirmed replacement order identifier.
    pub fn replacedBy(self: *const SingleLegOMS, order_id: []const u8) ?[]const u8 {
        return linkTarget(self.replaced_by_links.items, order_id);
    }

    /// Return the order identifier replaced by this order.
    pub fn replaces(self: *const SingleLegOMS, order_id: []const u8) ?[]const u8 {
        return linkTarget(self.replaces_links.items, order_id);
    }

    /// Return the confirmed replacement chain containing ``order_id``. The
    /// caller owns ``out``.
    pub fn replacementChain(self: *const SingleLegOMS, order_id: []const u8, out: *std.ArrayList([]const u8)) Error!void {
        _ = try self.machineIndex(order_id);
        var first = order_id;
        while (linkTarget(self.replaces_links.items, first)) |previous| first = previous;
        try out.append(self.gpa, first);
        while (linkTarget(self.replaced_by_links.items, out.items[out.items.len - 1])) |next| {
            try out.append(self.gpa, next);
        }
    }

    /// Return active orders in deterministic registration order. The caller
    /// owns ``out``; snapshots borrow the OMS's strings.
    pub fn activeStates(self: *const SingleLegOMS, out: *std.ArrayList(OrderSnapshot)) Error!void {
        for (self.machines.items) |*entry| {
            if (entry.machine.snapshot.isActive()) {
                try out.append(self.gpa, entry.machine.snapshot);
            }
        }
    }

    /// Describe session-close handling without mutating order state: Day and
    /// still-open IOC orders require an expiry report, GTC orders carry with
    /// no synthetic transition, and locally new orders get no directive.
    pub fn sessionDirectives(self: *const SingleLegOMS, out: *std.ArrayList(SessionDirective)) Error!void {
        for (self.machines.items) |*entry| {
            const snap = entry.machine.snapshot;
            if (!snap.isActive() or snap.status == .new) continue;
            try out.append(self.gpa, .{
                .order_id = snap.order.order_id,
                .action = if (snap.order.time_in_force == .gtc) .carry else .expire,
            });
        }
    }

    /// Return IOC orders requiring expiry after a matching opportunity.
    pub fn iocExpiryDirectives(self: *const SingleLegOMS, out: *std.ArrayList(SessionDirective)) Error!void {
        for (self.machines.items) |*entry| {
            const snap = entry.machine.snapshot;
            if (snap.order.time_in_force != .ioc) continue;
            if (!snap.isActive() or snap.status == .new) continue;
            try out.append(self.gpa, .{
                .order_id = snap.order.order_id,
                .action = .expire,
            });
        }
    }

    /// Apply a Day/IOC session expiry report, refusing to expire GTC.
    pub fn applySessionExpiry(self: *SingleLegOMS, report: ExecutionReport) Error!OrderSnapshot {
        try validateExpiryReport(report);
        const snapshot = try self.state(report.order_id);
        if (snapshot.timeInForce() == .gtc) return error.ExpiryPolicy;
        return self.apply(report);
    }

    /// Apply the expiry report emitted after an IOC matching opportunity.
    pub fn applyIocExpiry(self: *SingleLegOMS, report: ExecutionReport) Error!OrderSnapshot {
        try validateExpiryReport(report);
        const snapshot = try self.state(report.order_id);
        if (snapshot.timeInForce() != .ioc) return error.ExpiryPolicy;
        return self.apply(report);
    }

    fn machineIndex(self: *const SingleLegOMS, order_id: []const u8) Error!usize {
        for (self.machines.items, 0..) |entry, index| {
            if (std.mem.eql(u8, entry.machine.snapshot.order.order_id, order_id)) return index;
        }
        return error.UnknownOrder;
    }

    fn findUsedOrderId(self: *const SingleLegOMS, order_id: []const u8) ?usize {
        for (self.used_order_ids.items, 0..) |used, index| {
            if (std.mem.eql(u8, used, order_id)) return index;
        }
        return null;
    }

    fn replacementIndex(self: *const SingleLegOMS, original_order_id: []const u8) ?usize {
        for (self.replacements.items, 0..) |entry, index| {
            if (std.mem.eql(u8, entry.original_order_id, original_order_id)) return index;
        }
        return null;
    }

    fn linkTarget(links: []const LinkEntry, from: []const u8) ?[]const u8 {
        for (links) |link| {
            if (std.mem.eql(u8, link.from, from)) return link.to;
        }
        return null;
    }

    fn validateExpiryReport(report: ExecutionReport) Error!void {
        if (report.report_id.len == 0 or report.order_id.len == 0) return error.InvalidArgument;
        if (report.execution_type != .expired or report.status != .expired) {
            return error.ExpiryPolicy;
        }
    }
};

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/oms/test_manager.py.
// ---------------------------------------------------------------------------

const testing = std.testing;
const ten = state.ten;

fn managerOrder(
    order_id: []const u8,
    time_in_force: state.TimeInForce,
    side: state.Side,
    symbol: []const u8,
    quantity: i128,
) SingleLegOrder {
    return .{
        .order_id = order_id,
        .symbol = symbol,
        .side = side,
        .quantity = quantity,
        .order_type = .limit,
        .time_in_force = time_in_force,
    };
}

fn omsReport(
    oms: *SingleLegOMS,
    order_id: []const u8,
    status: OrderStatus,
    execution_type: state.ExecutionType,
    report_id: []const u8,
    last_quantity: i128,
    last_price: ?i128,
    average: state.AverageArg,
) Error!ExecutionReport {
    return state.reportFrom(
        try oms.state(order_id),
        status,
        execution_type,
        report_id,
        last_quantity,
        last_price,
        average,
    );
}

fn acknowledge(oms: *SingleLegOMS, order_id: []const u8, report_id: []const u8) Error!void {
    _ = try oms.apply(try omsReport(oms, order_id, .accepted, .acknowledged, report_id, 0, null, computed_average));
}

fn expireReport(oms: *SingleLegOMS, order_id: []const u8, report_id: []const u8) Error!ExecutionReport {
    return omsReport(oms, order_id, .expired, .expired, report_id, 0, null, computed_average);
}

test "replace is reserved then confirmed atomically by report" {
    var oms = SingleLegOMS.init(testing.allocator);
    defer oms.deinit();
    _ = try oms.submit(state.testOrder("original"));
    try acknowledge(&oms, "original", "ack-original");
    _ = try oms.apply(try omsReport(
        &oms,
        "original",
        .partially_filled,
        .partial_fill,
        "partial-original",
        1_250_000_000_000_000_000,
        dec.fromInt(100) + 100_000_000_000_000_000,
        computed_average,
    ));
    const replacement = managerOrder("replacement", .day, .buy, "AAPL", dec.fromInt(6));

    const pending = try oms.requestReplace("original", replacement);
    try testing.expectEqual(ReplaceStatus.pending, pending.status);
    try testing.expectEqual(OrderStatus.partially_filled, (try oms.state("original")).status);
    try testing.expect(oms.replacedBy("original") == null);
    try testing.expectError(error.UnknownOrder, oms.state("replacement"));

    const replaced = try oms.apply(try omsReport(
        &oms,
        "original",
        .replaced,
        .replaced,
        "replaced-original",
        0,
        null,
        computed_average,
    ));

    try testing.expectEqual(OrderStatus.replaced, replaced.status);
    try testing.expectEqual(@as(i128, 1_250_000_000_000_000_000), replaced.filled_quantity);
    try testing.expectEqual(@as(i128, 100_100_000_000_000_000_000), replaced.average_fill_price.?);
    try testing.expectEqual(OrderStatus.new, (try oms.state("replacement")).status);
    try testing.expectEqualStrings("replacement", oms.replacedBy("original").?);
    try testing.expectEqualStrings("original", oms.replaces("replacement").?);

    var chain: std.ArrayList([]const u8) = .empty;
    defer chain.deinit(testing.allocator);
    try oms.replacementChain("replacement", &chain);
    try testing.expectEqual(@as(usize, 2), chain.items.len);
    try testing.expectEqualStrings("original", chain.items[0]);
    try testing.expectEqualStrings("replacement", chain.items[1]);

    const applied = oms.replacement("original").?;
    try testing.expectEqual(ReplaceStatus.applied, applied.status);
    try testing.expectEqualStrings("replaced-original", applied.report_id.?);

    var active: std.ArrayList(OrderSnapshot) = .empty;
    defer active.deinit(testing.allocator);
    try oms.activeStates(&active);
    try testing.expectEqual(@as(usize, 1), active.items.len);
    try testing.expectEqualStrings("replacement", active.items[0].orderId());
}

test "failed replace report changes nothing and keeps reservation" {
    var oms = SingleLegOMS.init(testing.allocator);
    defer oms.deinit();
    _ = try oms.submit(state.testOrder("original"));
    try acknowledge(&oms, "original", "ack-original");
    _ = try oms.apply(try omsReport(
        &oms,
        "original",
        .partially_filled,
        .partial_fill,
        "partial",
        dec.one,
        dec.fromInt(100),
        computed_average,
    ));
    _ = try oms.requestReplace("original", state.testOrder("replacement"));
    const before = try oms.state("original");
    const invalid = try omsReport(
        &oms,
        "original",
        .replaced,
        .replaced,
        "bad-replaced",
        0,
        null,
        .{ .explicit = dec.fromInt(99) },
    );

    try testing.expectError(error.ExecutionLedger, oms.apply(invalid));

    try testing.expect(state.snapshotsEqual(before, try oms.state("original")));
    const pending = oms.replacement("original").?;
    try testing.expectEqual(ReplaceStatus.pending, pending.status);
    try testing.expectError(error.UnknownOrder, oms.state("replacement"));
}

test "replace requires accepted compatible order and pending request" {
    var oms = SingleLegOMS.init(testing.allocator);
    defer oms.deinit();
    _ = try oms.submit(state.testOrder("original"));

    try testing.expectError(
        error.ReplaceConflict,
        oms.requestReplace("original", state.testOrder("too-early")),
    );

    try acknowledge(&oms, "original", "ack-original");
    try testing.expectError(
        error.ReplaceConflict,
        oms.requestReplace("original", managerOrder("wrong-symbol", .day, .buy, "MSFT", ten)),
    );
    try testing.expectError(
        error.ReplaceConflict,
        oms.requestReplace("original", managerOrder("wrong-side", .day, .sell, "AAPL", ten)),
    );

    const without_pending = try omsReport(
        &oms,
        "original",
        .replaced,
        .replaced,
        "without-pending",
        0,
        null,
        computed_average,
    );
    try testing.expectError(error.ReplaceConflict, oms.apply(without_pending));
    try testing.expectEqual(OrderStatus.accepted, (try oms.state("original")).status);
}

test "terminal report aborts pending replace and preserves cancel ledger" {
    var oms = SingleLegOMS.init(testing.allocator);
    defer oms.deinit();
    _ = try oms.submit(state.testOrder("original"));
    try acknowledge(&oms, "original", "ack-original");
    _ = try oms.requestReplace("original", state.testOrder("reserved"));

    _ = try oms.apply(try omsReport(
        &oms,
        "original",
        .cancel_pending,
        .status,
        "cancel-pending",
        0,
        null,
        computed_average,
    ));
    const canceled = try oms.apply(try omsReport(
        &oms,
        "original",
        .canceled,
        .canceled,
        "canceled",
        0,
        null,
        computed_average,
    ));

    try testing.expectEqual(@as(i128, 0), canceled.filled_quantity);
    try testing.expectEqual(ten, canceled.remaining_quantity);
    const replacement = oms.replacement("original").?;
    try testing.expectEqual(ReplaceStatus.aborted, replacement.status);
    try testing.expectEqualStrings("canceled", replacement.report_id.?);
    try testing.expectError(error.UnknownOrder, oms.state("reserved"));
}

test "ioc expiry and gtc session carry over" {
    var oms = SingleLegOMS.init(testing.allocator);
    defer oms.deinit();
    _ = try oms.submit(managerOrder("local-new", .gtc, .buy, "AAPL", ten));
    _ = try oms.submit(managerOrder("day", .day, .buy, "AAPL", ten));
    _ = try oms.submit(managerOrder("gtc", .gtc, .buy, "AAPL", ten));
    _ = try oms.submit(managerOrder("ioc", .ioc, .buy, "AAPL", ten));
    try acknowledge(&oms, "day", "ack-day");
    try acknowledge(&oms, "gtc", "ack-gtc");
    try acknowledge(&oms, "ioc", "ack-ioc");

    var directives: std.ArrayList(SessionDirective) = .empty;
    defer directives.deinit(testing.allocator);
    try oms.sessionDirectives(&directives);
    try testing.expectEqual(@as(usize, 3), directives.items.len);
    try testing.expectEqualStrings("day", directives.items[0].order_id);
    try testing.expectEqual(SessionAction.expire, directives.items[0].action);
    try testing.expectEqualStrings("gtc", directives.items[1].order_id);
    try testing.expectEqual(SessionAction.carry, directives.items[1].action);
    try testing.expectEqualStrings("ioc", directives.items[2].order_id);
    try testing.expectEqual(SessionAction.expire, directives.items[2].action);

    var ioc_directives: std.ArrayList(SessionDirective) = .empty;
    defer ioc_directives.deinit(testing.allocator);
    try oms.iocExpiryDirectives(&ioc_directives);
    try testing.expectEqual(@as(usize, 1), ioc_directives.items.len);
    try testing.expectEqualStrings("ioc", ioc_directives.items[0].order_id);
    try testing.expectEqual(SessionAction.expire, ioc_directives.items[0].action);

    _ = try oms.apply(try omsReport(
        &oms,
        "ioc",
        .partially_filled,
        .partial_fill,
        "ioc-partial",
        2_500_000_000_000_000_000,
        dec.fromInt(100) + 50_000_000_000_000_000,
        computed_average,
    ));
    const ioc_expired = try oms.applyIocExpiry(try expireReport(&oms, "ioc", "expired-ioc"));
    const day_expired = try oms.applySessionExpiry(try expireReport(&oms, "day", "expired-day"));

    try testing.expectEqual(OrderStatus.expired, ioc_expired.status);
    try testing.expectEqual(@as(i128, 2_500_000_000_000_000_000), ioc_expired.filled_quantity);
    try testing.expectEqual(@as(i128, 7_500_000_000_000_000_000), ioc_expired.remaining_quantity);
    try testing.expectEqual(@as(i128, 100_050_000_000_000_000_000), ioc_expired.average_fill_price.?);
    try testing.expectEqual(OrderStatus.expired, day_expired.status);
    try testing.expectEqual(OrderStatus.accepted, (try oms.state("gtc")).status);
    try testing.expectEqual(OrderStatus.new, (try oms.state("local-new")).status);

    var later_directives: std.ArrayList(SessionDirective) = .empty;
    defer later_directives.deinit(testing.allocator);
    try oms.sessionDirectives(&later_directives);
    try testing.expectEqual(@as(usize, 1), later_directives.items.len);
    try testing.expectEqualStrings("gtc", later_directives.items[0].order_id);
    try testing.expectEqual(SessionAction.carry, later_directives.items[0].action);

    // A later-session report continues the same GTC ledger.
    const filled = try oms.apply(try omsReport(
        &oms,
        "gtc",
        .filled,
        .fill,
        "gtc-next-session-fill",
        ten,
        dec.fromInt(102) + 150_000_000_000_000_000,
        computed_average,
    ));
    try testing.expectEqual(OrderStatus.filled, filled.status);
    try testing.expectEqual(@as(i128, 102_150_000_000_000_000_000), filled.average_fill_price.?);
}

test "expiry policy refusals" {
    var oms = SingleLegOMS.init(testing.allocator);
    defer oms.deinit();
    _ = try oms.submit(managerOrder("gtc", .gtc, .buy, "AAPL", ten));
    _ = try oms.submit(managerOrder("day", .day, .buy, "AAPL", ten));
    try acknowledge(&oms, "gtc", "ack-gtc");
    try acknowledge(&oms, "day", "ack-day");

    try testing.expectError(
        error.ExpiryPolicy,
        oms.applySessionExpiry(try expireReport(&oms, "gtc", "expired-gtc")),
    );
    try testing.expectError(
        error.ExpiryPolicy,
        oms.applyIocExpiry(try expireReport(&oms, "day", "expired-day")),
    );
    try testing.expectError(
        error.ExpiryPolicy,
        oms.applyIocExpiry(try omsReport(
            &oms,
            "day",
            .canceled,
            .canceled,
            "not-expiry",
            0,
            null,
            computed_average,
        )),
    );

    try testing.expectEqual(OrderStatus.accepted, (try oms.state("gtc")).status);
    try testing.expectEqual(OrderStatus.accepted, (try oms.state("day")).status);
}
