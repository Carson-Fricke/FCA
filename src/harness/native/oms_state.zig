//! Execution-report-driven order state machine, ported from
//! ``src/harness/oms/state.py`` (the ``SingleLegOMS`` registry lives in
//! ``oms_manager.zig``).
//!
//! Every ledger quantity is the pinned i128 fixed-point type (see
//! ``decimal.zig``). Status transitions happen only by applying validated
//! execution reports, exactly like ``OrderStateMachine``; the transition and
//! ledger tables are mirrored line-for-line from the Python module.
//!
//! Envelope fields of the Python domain records (``event_id``,
//! ``timestamp``, ``sequence``) ride on the bus ``Event`` (see ``bus.zig``);
//! the state machine is driven purely by the report payload, as in Python.

const std = @import("std");
pub const dec = @import("decimal.zig").dec;

pub const Error = error{
    OutOfMemory,
    InvalidArgument,
    /// Fixed-point overflow or a non-representable value (decimal128(38, 18)).
    OutOfRange,
    /// Division by zero or otherwise invalid decimal operation.
    InvalidDecimal,
    /// Order identifier is not tracked by the OMS (Python ``UnknownOrderError``).
    UnknownOrder,
    /// Order identifier already used or reserved (Python ``DuplicateOrderError``).
    DuplicateOrder,
    /// Execution report identifier applied more than once.
    DuplicateReport,
    /// Execution report cannot follow the current state.
    IllegalTransition,
    /// Cumulative or last-fill report values are inconsistent.
    ExecutionLedger,
    /// Replacement request or report is inconsistent (Python ``ReplaceError``).
    ReplaceConflict,
    /// Session/expiry policy refusal (Python ``OmsError`` on expiry paths).
    ExpiryPolicy,
};

pub const Side = enum { buy, sell };

pub const OrderType = enum { market, limit, stop, stop_limit };

pub const TimeInForce = enum { day, gtc, ioc };

pub const OrderStatus = enum {
    new,
    accepted,
    partially_filled,
    filled,
    cancel_pending,
    canceled,
    rejected,
    expired,
    replaced,
};

pub const ExecutionType = enum {
    acknowledged,
    partial_fill,
    fill,
    canceled,
    rejected,
    expired,
    replaced,
    status,
};

/// A single-leg order (Python ``SingleLegOrder``); envelope fields ride on
/// the bus event. ``quantity`` is unscaled at scale 18 and must be positive.
pub const SingleLegOrder = struct {
    order_id: []const u8,
    symbol: []const u8,
    side: Side,
    quantity: i128,
    order_type: OrderType = .market,
    time_in_force: TimeInForce = .day,
};

/// A venue/OMS report (Python ``ExecutionReport``); all quantities are
/// unscaled at scale 18. Validated by the state machine on application.
pub const ExecutionReport = struct {
    report_id: []const u8,
    order_id: []const u8,
    status: OrderStatus,
    execution_type: ExecutionType,
    order_quantity: i128,
    filled_quantity: i128,
    remaining_quantity: i128,
    average_fill_price: ?i128 = null,
    last_fill_quantity: i128 = 0,
    last_fill_price: ?i128 = null,
};

/// Immutable lifecycle and exact execution ledger for one order.
pub const OrderSnapshot = struct {
    order: SingleLegOrder,
    status: OrderStatus,
    filled_quantity: i128,
    remaining_quantity: i128,
    filled_notional: i128,
    average_fill_price: ?i128,
    report_count: usize,
    last_report: ?ExecutionReport,

    pub fn orderId(self: *const OrderSnapshot) []const u8 {
        return self.order.order_id;
    }

    pub fn isActive(self: *const OrderSnapshot) bool {
        return !isTerminalStatus(self.status);
    }

    pub fn isTerminal(self: *const OrderSnapshot) bool {
        return isTerminalStatus(self.status);
    }

    pub fn timeInForce(self: *const OrderSnapshot) TimeInForce {
        return self.order.time_in_force;
    }
};

pub fn isTerminalStatus(status: OrderStatus) bool {
    return switch (status) {
        .filled, .canceled, .rejected, .expired, .replaced => true,
        else => false,
    };
}

/// Statuses each execution type may declare (Python ``_EXECUTION_STATUSES``).
pub fn executionStatuses(execution_type: ExecutionType) []const OrderStatus {
    return switch (execution_type) {
        .acknowledged => &.{.accepted},
        .partial_fill => &.{.partially_filled},
        .fill => &.{.filled},
        .canceled => &.{.canceled},
        .rejected => &.{.rejected},
        .expired => &.{.expired},
        .replaced => &.{.replaced},
        .status => &.{ .new, .accepted, .partially_filled, .cancel_pending },
    };
}

/// Transitions keyed by execution type as well as status, mirroring Python's
/// ``_ALLOWED_TRANSITIONS``. This prevents a second acknowledgement from
/// masquerading as an accepted-status heartbeat.
fn legalTransitions(from: OrderStatus, via: ExecutionType) []const OrderStatus {
    return switch (from) {
        .new => switch (via) {
            .status => &.{.new},
            .acknowledged => &.{.accepted},
            .rejected => &.{.rejected},
            else => &.{},
        },
        .accepted => switch (via) {
            .status => &.{ .accepted, .cancel_pending },
            .partial_fill => &.{.partially_filled},
            .fill => &.{.filled},
            .canceled => &.{.canceled},
            .expired => &.{.expired},
            .replaced => &.{.replaced},
            else => &.{},
        },
        .partially_filled => switch (via) {
            .status => &.{ .partially_filled, .cancel_pending },
            .partial_fill => &.{.partially_filled},
            .fill => &.{.filled},
            .canceled => &.{.canceled},
            .expired => &.{.expired},
            .replaced => &.{.replaced},
            else => &.{},
        },
        .cancel_pending => switch (via) {
            .status => &.{ .cancel_pending, .accepted, .partially_filled },
            .partial_fill => &.{.partially_filled},
            .fill => &.{.filled},
            .canceled => &.{.canceled},
            .expired => &.{.expired},
            else => &.{},
        },
        .filled, .canceled, .rejected, .expired, .replaced => &.{},
    };
}

pub fn containsStatus(haystack: []const OrderStatus, needle: OrderStatus) bool {
    for (haystack) |status| {
        if (status == needle) return true;
    }
    return false;
}

/// Apply validated execution reports to one single-leg order. Construction
/// establishes only the local ``NEW`` state; ``apply`` is the single mutating
/// entrypoint and is atomic: a rejected report leaves the machine unchanged.
pub const OrderStateMachine = struct {
    gpa: std.mem.Allocator,
    snapshot: OrderSnapshot,
    reports: std.ArrayList(ExecutionReport) = .empty,

    pub fn init(gpa: std.mem.Allocator, order: SingleLegOrder) OrderStateMachine {
        return .{
            .gpa = gpa,
            .snapshot = .{
                .order = order,
                .status = .new,
                .filled_quantity = 0,
                .remaining_quantity = order.quantity,
                .filled_notional = 0,
                .average_fill_price = null,
                .report_count = 0,
                .last_report = null,
            },
        };
    }

    pub fn deinit(self: *OrderStateMachine) void {
        self.reports.deinit(self.gpa);
        self.reports = .empty;
    }

    /// Validate and atomically apply one execution report.
    pub fn apply(self: *OrderStateMachine, report: ExecutionReport) Error!OrderSnapshot {
        if (report.report_id.len == 0) return error.InvalidArgument;
        for (self.reports.items) |applied| {
            if (std.mem.eql(u8, applied.report_id, report.report_id)) {
                return error.DuplicateReport;
            }
        }
        const next = try self.nextSnapshot(report);
        try self.reports.append(self.gpa, report);
        self.snapshot = next;
        return next;
    }

    fn nextSnapshot(self: *const OrderStateMachine, report: ExecutionReport) Error!OrderSnapshot {
        const current = self.snapshot;
        if (report.order_id.len == 0) return error.InvalidArgument;
        if (!std.mem.eql(u8, report.order_id, current.order.order_id)) {
            return error.ExecutionLedger;
        }
        if (report.order_quantity != current.order.quantity) {
            return error.ExecutionLedger;
        }
        if (!containsStatus(executionStatuses(report.execution_type), report.status)) {
            return error.IllegalTransition;
        }
        if (!containsStatus(legalTransitions(current.status, report.execution_type), report.status)) {
            return error.IllegalTransition;
        }
        const filled_notional = try validatedNotional(current, report);
        return .{
            .order = current.order,
            .status = report.status,
            .filled_quantity = report.filled_quantity,
            .remaining_quantity = report.remaining_quantity,
            .filled_notional = filled_notional,
            .average_fill_price = report.average_fill_price,
            .report_count = current.report_count + 1,
            .last_report = report,
        };
    }

    fn validatedNotional(current: OrderSnapshot, report: ExecutionReport) Error!i128 {
        const is_fill = report.execution_type == .partial_fill or report.execution_type == .fill;
        var filled_notional: i128 = undefined;

        if (is_fill) {
            if (report.last_fill_quantity <= 0) return error.ExecutionLedger;
            if (report.last_fill_price == null) return error.ExecutionLedger;
            const expected_filled = try dec.add(current.filled_quantity, report.last_fill_quantity);
            if (report.filled_quantity != expected_filled) {
                return error.ExecutionLedger;
            }
            filled_notional = try dec.add(
                current.filled_notional,
                try dec.mul(report.last_fill_quantity, report.last_fill_price.?),
            );
        } else {
            if (report.last_fill_quantity != 0) return error.ExecutionLedger;
            if (report.last_fill_price != null) return error.ExecutionLedger;
            if (report.filled_quantity != current.filled_quantity) {
                return error.ExecutionLedger;
            }
            filled_notional = current.filled_notional;
        }

        const expected_remaining = try dec.sub(current.order.quantity, report.filled_quantity);
        if (report.remaining_quantity != expected_remaining) {
            return error.ExecutionLedger;
        }

        if (report.filled_quantity == 0) {
            if (report.average_fill_price != null) return error.ExecutionLedger;
        } else {
            const expected_average = try dec.div(filled_notional, report.filled_quantity);
            if (report.average_fill_price == null or report.average_fill_price.? != expected_average) {
                return error.ExecutionLedger;
            }
        }

        if (report.status == .partially_filled and
            !(report.filled_quantity > 0 and report.filled_quantity < current.order.quantity))
        {
            return error.ExecutionLedger;
        }
        if (report.status == .filled and report.filled_quantity != current.order.quantity) {
            return error.ExecutionLedger;
        }
        if ((report.status == .new or report.status == .accepted or report.status == .rejected) and
            report.filled_quantity != 0)
        {
            return error.ExecutionLedger;
        }
        return filled_notional;
    }
};

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/oms/test_state.py. The report
// builders below are shared with the ``oms_manager`` tests.
// ---------------------------------------------------------------------------

const testing = std.testing;

pub const ten = dec.fromInt(10);

pub fn testOrder(order_id: []const u8) SingleLegOrder {
    return .{
        .order_id = order_id,
        .symbol = "AAPL",
        .side = .buy,
        .quantity = ten,
        .order_type = .limit,
        .time_in_force = .day,
    };
}

/// Distinguishes "compute the ledger-exact average" from an explicit value
/// (the Python tests pass a ``UNSET`` sentinel for the same purpose).
pub const AverageArg = union(enum) { computed, explicit: ?i128 };
pub const computed_average = AverageArg.computed;

pub fn reportFrom(
    snapshot: OrderSnapshot,
    status: OrderStatus,
    execution_type: ExecutionType,
    report_id: []const u8,
    last_quantity: i128,
    last_price: ?i128,
    average: AverageArg,
) Error!ExecutionReport {
    var notional = snapshot.filled_notional;
    if (last_price) |price| {
        notional = try dec.add(notional, try dec.mul(last_quantity, price));
    }
    const filled = try dec.add(snapshot.filled_quantity, last_quantity);
    const calculated: ?i128 = if (filled == 0) null else try dec.div(notional, filled);
    return .{
        .report_id = report_id,
        .order_id = snapshot.order.order_id,
        .status = status,
        .execution_type = execution_type,
        .order_quantity = snapshot.order.quantity,
        .filled_quantity = filled,
        .remaining_quantity = try dec.sub(snapshot.order.quantity, filled),
        .average_fill_price = switch (average) {
            .computed => calculated,
            .explicit => |value| value,
        },
        .last_fill_quantity = last_quantity,
        .last_fill_price = last_price,
    };
}

fn reportFor(
    machine: *const OrderStateMachine,
    status: OrderStatus,
    execution_type: ExecutionType,
    report_id: []const u8,
    last_quantity: i128,
    last_price: ?i128,
    average: AverageArg,
) Error!ExecutionReport {
    return reportFrom(machine.snapshot, status, execution_type, report_id, last_quantity, last_price, average);
}

fn applyAck(machine: *OrderStateMachine, report_id: []const u8) Error!void {
    _ = try machine.apply(try reportFor(machine, .accepted, .acknowledged, report_id, 0, null, computed_average));
}

fn applyPartial(machine: *OrderStateMachine, report_id: []const u8) Error!void {
    _ = try machine.apply(try reportFor(
        machine,
        .partially_filled,
        .partial_fill,
        report_id,
        dec.fromInt(2),
        dec.fromInt(100) + 250_000_000_000_000_000,
        computed_average,
    ));
}

fn machineInStatus(comptime status: OrderStatus, gpa: std.mem.Allocator) Error!OrderStateMachine {
    var machine = OrderStateMachine.init(gpa, testOrder("source-" ++ @tagName(status)));
    errdefer machine.deinit();
    switch (status) {
        .new => {},
        .rejected => _ = try machine.apply(try reportFor(&machine, .rejected, .rejected, "setup-rejected", 0, null, computed_average)),
        .accepted => try applyAck(&machine, "setup-ack"),
        .partially_filled => {
            try applyAck(&machine, "setup-ack");
            try applyPartial(&machine, "setup-partial");
        },
        .cancel_pending => {
            try applyAck(&machine, "setup-ack");
            _ = try machine.apply(try reportFor(&machine, .cancel_pending, .status, "setup-cancel-pending", 0, null, computed_average));
        },
        .filled => {
            try applyAck(&machine, "setup-ack");
            _ = try machine.apply(try reportFor(&machine, .filled, .fill, "setup-filled", ten, dec.fromInt(100), computed_average));
        },
        .canceled => {
            try applyAck(&machine, "setup-ack");
            _ = try machine.apply(try reportFor(&machine, .canceled, .canceled, "setup-canceled", 0, null, computed_average));
        },
        .expired => {
            try applyAck(&machine, "setup-ack");
            _ = try machine.apply(try reportFor(&machine, .expired, .expired, "setup-expired", 0, null, computed_average));
        },
        .replaced => {
            try applyAck(&machine, "setup-ack");
            _ = try machine.apply(try reportFor(&machine, .replaced, .replaced, "setup-replaced", 0, null, computed_average));
        },
    }
    return machine;
}

fn candidateReport(comptime target: OrderStatus, machine: *const OrderStateMachine) Error!ExecutionReport {
    const execution_type: ExecutionType = switch (target) {
        .new => .status,
        .accepted => if (machine.snapshot.status == .new) ExecutionType.acknowledged else ExecutionType.status,
        .partially_filled => .partial_fill,
        .filled => .fill,
        .cancel_pending => .status,
        .canceled => .canceled,
        .rejected => .rejected,
        .expired => .expired,
        .replaced => .replaced,
    };
    var last_quantity: i128 = 0;
    var last_price: ?i128 = null;
    switch (target) {
        .partially_filled => {
            if (machine.snapshot.remaining_quantity > 0) {
                last_quantity = @min(dec.one, machine.snapshot.remaining_quantity);
                last_price = dec.fromInt(100);
            }
        },
        .filled => {
            last_quantity = machine.snapshot.remaining_quantity;
            if (last_quantity > 0) last_price = dec.fromInt(100);
        },
        else => {},
    }
    return reportFor(machine, target, execution_type, "candidate-" ++ @tagName(target), last_quantity, last_price, computed_average);
}

fn legalTargets(comptime source: OrderStatus) []const OrderStatus {
    return switch (source) {
        .new => &.{ .new, .accepted, .rejected },
        .accepted => &.{ .accepted, .partially_filled, .filled, .cancel_pending, .canceled, .expired, .replaced },
        .partially_filled => &.{ .partially_filled, .filled, .cancel_pending, .canceled, .expired, .replaced },
        .cancel_pending => &.{ .accepted, .partially_filled, .filled, .cancel_pending, .canceled, .expired },
        .filled, .canceled, .rejected, .expired, .replaced => &.{},
    };
}

pub fn snapshotsEqual(a: OrderSnapshot, b: OrderSnapshot) bool {
    return a.status == b.status and
        a.filled_quantity == b.filled_quantity and
        a.remaining_quantity == b.remaining_quantity and
        a.filled_notional == b.filled_notional and
        a.average_fill_price == b.average_fill_price and
        a.report_count == b.report_count and
        ((a.last_report == null) == (b.last_report == null));
}

test "all order types and time-in-force values share the lifecycle" {
    inline for (@typeInfo(OrderType).@"enum".fields) |type_field| {
        inline for (@typeInfo(TimeInForce).@"enum".fields) |tif_field| {
            var machine = OrderStateMachine.init(testing.allocator, .{
                .order_id = type_field.name ++ "-" ++ tif_field.name,
                .symbol = "AAPL",
                .side = .buy,
                .quantity = ten,
                .order_type = @enumFromInt(type_field.value),
                .time_in_force = @enumFromInt(tif_field.value),
            });
            defer machine.deinit();

            try applyAck(&machine, "ack");
            const partial = try machine.apply(try reportFor(
                &machine,
                .partially_filled,
                .partial_fill,
                "partial",
                dec.fromInt(3),
                dec.fromInt(100),
                computed_average,
            ));
            const filled = try machine.apply(try reportFor(
                &machine,
                .filled,
                .fill,
                "fill",
                dec.fromInt(7),
                dec.fromInt(101),
                computed_average,
            ));

            try testing.expectEqual(OrderStatus.partially_filled, partial.status);
            try testing.expectEqual(OrderStatus.filled, filled.status);
            try testing.expectEqual(ten, filled.filled_quantity);
            try testing.expectEqual(@as(i128, 100_700_000_000_000_000_000), filled.average_fill_price.?);
            try testing.expectEqual(@as(OrderType, @enumFromInt(type_field.value)), machine.snapshot.order.order_type);
            try testing.expectEqual(@as(TimeInForce, @enumFromInt(tif_field.value)), machine.snapshot.order.time_in_force);
        }
    }
}

test "partial fills and cancel preserve exact decimal ledgers" {
    var machine = OrderStateMachine.init(testing.allocator, testOrder("order-1"));
    defer machine.deinit();
    try applyAck(&machine, "ack");
    _ = try machine.apply(try reportFor(
        &machine,
        .partially_filled,
        .partial_fill,
        "partial-1",
        125_000_000_000_000_000,
        10_010_000_000_000_000_000,
        computed_average,
    ));
    const partial = try machine.apply(try reportFor(
        &machine,
        .partially_filled,
        .partial_fill,
        "partial-2",
        375_000_000_000_000_000,
        10_030_000_000_000_000_000,
        computed_average,
    ));
    _ = try machine.apply(try reportFor(
        &machine,
        .cancel_pending,
        .status,
        "cancel-pending",
        0,
        null,
        computed_average,
    ));
    const canceled = try machine.apply(try reportFor(
        &machine,
        .canceled,
        .canceled,
        "canceled",
        0,
        null,
        computed_average,
    ));

    try testing.expectEqual(@as(i128, 500_000_000_000_000_000), partial.filled_quantity);
    try testing.expectEqual(@as(i128, 9_500_000_000_000_000_000), partial.remaining_quantity);
    try testing.expectEqual(@as(i128, 5_012_500_000_000_000_000), partial.filled_notional);
    try testing.expectEqual(@as(i128, 10_025_000_000_000_000_000), partial.average_fill_price.?);
    try testing.expectEqual(partial.filled_quantity, canceled.filled_quantity);
    try testing.expectEqual(partial.remaining_quantity, canceled.remaining_quantity);
    try testing.expectEqual(partial.filled_notional, canceled.filled_notional);
    try testing.expectEqual(partial.average_fill_price, canceled.average_fill_price);
}

test "invalid report is atomic and duplicate report id is rejected" {
    var machine = OrderStateMachine.init(testing.allocator, testOrder("order-1"));
    defer machine.deinit();
    try applyAck(&machine, "ack");
    const before = machine.snapshot;

    const bad_average = try reportFor(
        &machine,
        .partially_filled,
        .partial_fill,
        "bad-average",
        dec.one,
        dec.fromInt(100),
        .{ .explicit = dec.fromInt(99) + 990_000_000_000_000_000 },
    );
    try testing.expectError(error.ExecutionLedger, machine.apply(bad_average));
    try testing.expect(snapshotsEqual(before, machine.snapshot));
    try testing.expectEqual(@as(usize, 1), machine.reports.items.len);

    const duplicate = try reportFor(&machine, .accepted, .status, "ack", 0, null, computed_average);
    try testing.expectError(error.DuplicateReport, machine.apply(duplicate));
    try testing.expect(snapshotsEqual(before, machine.snapshot));
}

test "execution type cannot masquerade as another status" {
    const cases = [_]struct { execution_type: ExecutionType, status: OrderStatus }{
        .{ .execution_type = .acknowledged, .status = .canceled },
        .{ .execution_type = .partial_fill, .status = .filled },
        .{ .execution_type = .fill, .status = .partially_filled },
        .{ .execution_type = .canceled, .status = .expired },
        .{ .execution_type = .rejected, .status = .accepted },
        .{ .execution_type = .expired, .status = .canceled },
        .{ .execution_type = .replaced, .status = .canceled },
        .{ .execution_type = .status, .status = .filled },
    };
    for (cases) |case| {
        var machine = OrderStateMachine.init(testing.allocator, testOrder("order-1"));
        defer machine.deinit();
        const before = machine.snapshot;
        const mismatch = try reportFor(
            &machine,
            case.status,
            case.execution_type,
            "mismatch",
            0,
            null,
            computed_average,
        );
        try testing.expectError(error.IllegalTransition, machine.apply(mismatch));
        try testing.expect(snapshotsEqual(before, machine.snapshot));
    }
}

test "transition matrix is exhaustive" {
    inline for (@typeInfo(OrderStatus).@"enum".fields) |source_field| {
        inline for (@typeInfo(OrderStatus).@"enum".fields) |target_field| {
            const source: OrderStatus = @enumFromInt(source_field.value);
            const target: OrderStatus = @enumFromInt(target_field.value);
            var machine = try machineInStatus(source, testing.allocator);
            defer machine.deinit();
            const candidate = try candidateReport(target, &machine);
            if (containsStatus(legalTargets(source), target)) {
                const applied = try machine.apply(candidate);
                try testing.expectEqual(target, applied.status);
            } else {
                const before = machine.snapshot;
                try testing.expectError(error.IllegalTransition, machine.apply(candidate));
                try testing.expect(snapshotsEqual(before, machine.snapshot));
            }
        }
    }
}
