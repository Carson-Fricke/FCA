//! Decimal-exact accounting for one strategy's virtual book, ported from
//! ``src/harness/portfolio/book.py`` onto the pinned fixed-point type.
//!
//! Cash, positions, average cost, and realized/unrealized PnL use the
//! scale-18 i128 fixed-point kernel (``oms.dec``, ``decimal128(38, 18)``).
//! Fill prices determine average cost; fees, including negative rebate fees,
//! are recognized immediately in realized PnL; marks are explicit and never
//! mutate cost or realized PnL. Position ledgers are kept sorted by canonical
//! symbol bytes, matching Python's ``sorted(..., key=str)`` snapshot order.

const std = @import("std");
const oms = @import("oms.zig");

const dec = oms.dec;

pub const Error = error{
    OutOfMemory,
    InvalidArgument,
    /// Fixed-point overflow or a non-representable value (decimal128(38, 18)).
    OutOfRange,
    /// Division by zero or otherwise invalid decimal operation.
    InvalidDecimal,
    /// Fill identifier accounted more than once (Python ``DuplicateFillError``).
    DuplicateFill,
    /// Symbol never held by this book (Python ``UnknownPositionError``).
    UnknownPosition,
    /// Ledger is already flat (Python ``PortfolioError`` on retirement).
    PositionFlat,
};

pub const Side = oms.Side;

/// One immutable execution fill (Python ``Fill``); quantities are unscaled at
/// scale 18, ``quantity``/``price`` positive and ``fee`` any sign (negative
/// fees represent liquidity rebates).
pub const Fill = struct {
    fill_id: []const u8,
    order_id: []const u8,
    symbol: []const u8,
    side: Side,
    quantity: i128,
    price: i128,
    fee: i128 = 0,
};

/// An explicit mark used to value a position without consulting a clock.
pub const PositionMarker = struct {
    symbol: []const u8,
    price: i128,
};

/// Immutable signed position, cost basis, and attributed PnL.
pub const PositionSnapshot = struct {
    symbol: []const u8,
    quantity: i128,
    cost_basis: i128,
    cash_flow: i128,
    realized_pnl: i128,
    mark_price: ?i128,

    pub fn isFlat(self: *const PositionSnapshot) bool {
        return self.quantity == 0;
    }

    /// Average entry price, or zero for a flat position.
    pub fn averageCost(self: *const PositionSnapshot) Error!i128 {
        if (self.isFlat()) return 0;
        return dec.div(self.cost_basis, self.quantity);
    }

    /// Signed marked value; null only when open and unmarked.
    pub fn marketValue(self: *const PositionSnapshot) Error!?i128 {
        if (self.isFlat()) return 0;
        if (self.mark_price == null) return null;
        return try dec.mul(self.quantity, self.mark_price.?);
    }

    /// Exact marked change from this position's cash flows.
    pub fn totalPnl(self: *const PositionSnapshot) Error!?i128 {
        const market_value = try self.marketValue();
        if (market_value == null) return null;
        return try dec.add(self.cash_flow, market_value.?);
    }

    /// Marked PnL as the exact remainder after realized PnL.
    pub fn unrealizedPnl(self: *const PositionSnapshot) Error!?i128 {
        const total = try self.totalPnl();
        if (total == null) return null;
        return try dec.sub(total.?, self.realized_pnl);
    }
};

/// Immutable valuation of one strategy virtual book. ``positions`` borrows
/// the caller-provided list filled by ``VirtualBook.snapshot``.
pub const BookSnapshot = struct {
    book_id: []const u8,
    initial_cash: i128,
    cash: i128,
    positions: []const PositionSnapshot,
    fill_count: usize,

    /// Realized PnL across all symbols, including all fill fees.
    pub fn realizedPnl(self: *const BookSnapshot) Error!i128 {
        var total: i128 = 0;
        for (self.positions) |snapshot| {
            total = try dec.add(total, snapshot.realized_pnl);
        }
        return total;
    }

    /// Total unrealized PnL, or null if an open leg is unmarked.
    pub fn unrealizedPnl(self: *const BookSnapshot) Error!?i128 {
        const total = try self.totalPnl();
        if (total == null) return null;
        return try dec.sub(total.?, try self.realizedPnl());
    }

    /// Total signed market value when every open leg is marked.
    pub fn marketValue(self: *const BookSnapshot) Error!?i128 {
        var total: i128 = 0;
        for (self.positions) |snapshot| {
            const value = try snapshot.marketValue();
            if (value == null) return null;
            total = try dec.add(total, value.?);
        }
        return total;
    }

    /// Marked book equity, or null while a leg is unmarked.
    pub fn equity(self: *const BookSnapshot) Error!?i128 {
        const market_value = try self.marketValue();
        if (market_value == null) return null;
        return try dec.add(self.cash, market_value.?);
    }

    /// The exact marked change in book equity.
    pub fn totalPnl(self: *const BookSnapshot) Error!?i128 {
        const marked_equity = try self.equity();
        if (marked_equity == null) return null;
        return try dec.sub(marked_equity.?, self.initial_cash);
    }

    pub fn position(self: *const BookSnapshot, symbol: []const u8) Error!PositionSnapshot {
        for (self.positions) |snapshot| {
            if (std.mem.eql(u8, snapshot.symbol, symbol)) return snapshot;
        }
        return error.UnknownPosition;
    }
};

const Ledger = struct {
    symbol: []const u8,
    quantity: i128,
    cost_basis: i128,
    cash_flow: i128,
    realized_pnl: i128,
};

const MarkerEntry = struct {
    symbol: []const u8,
    price: i128,
};

/// Maintain cash and average-cost positions for one strategy, mirroring
/// Python's ``VirtualBook`` ledger algebra on the pinned fixed-point type.
pub const VirtualBook = struct {
    gpa: std.mem.Allocator,
    book_id: []const u8,
    initial_cash: i128,
    cash: i128,
    ledgers: std.ArrayList(Ledger) = .empty,
    markers: std.ArrayList(MarkerEntry) = .empty,
    fill_ids: std.ArrayList([]const u8) = .empty,

    pub fn init(gpa: std.mem.Allocator, book_id: []const u8, initial_cash: i128) VirtualBook {
        return .{
            .gpa = gpa,
            .book_id = book_id,
            .initial_cash = initial_cash,
            .cash = initial_cash,
        };
    }

    pub fn deinit(self: *VirtualBook) void {
        self.ledgers.deinit(self.gpa);
        self.markers.deinit(self.gpa);
        self.fill_ids.deinit(self.gpa);
    }

    /// Atomically account one immutable core fill. The ledger algebra is the
    /// exact port of ``VirtualBook.apply_fill``: same-sign or flat entries
    /// extend the basis, closes release proportional basis into realized PnL,
    /// reversals reprice the flipped remainder, fees hit realized PnL and
    /// cash immediately, and flat ledgers finalize realized PnL as the exact
    /// total cash flow.
    pub fn applyFill(self: *VirtualBook, fill: Fill) Error!PositionSnapshot {
        if (fill.fill_id.len == 0 or fill.order_id.len == 0 or fill.symbol.len == 0) {
            return error.InvalidArgument;
        }
        if (fill.quantity <= 0 or fill.price <= 0) return error.InvalidArgument;
        for (self.fill_ids.items) |seen| {
            if (std.mem.eql(u8, seen, fill.fill_id)) return error.DuplicateFill;
        }

        const signed: i128 = if (fill.side == .buy) fill.quantity else -fill.quantity;
        const index = self.ledgerIndex(fill.symbol);
        const quantity: i128 = if (index) |i| self.ledgers.items[i].quantity else 0;
        const cost_basis: i128 = if (index) |i| self.ledgers.items[i].cost_basis else 0;
        const cash_flow: i128 = if (index) |i| self.ledgers.items[i].cash_flow else 0;
        var realized_pnl: i128 = if (index) |i| self.ledgers.items[i].realized_pnl else 0;
        const next_quantity = try dec.add(quantity, signed);

        var next_cost_basis: i128 = undefined;
        if (quantity == 0 or @as(i256, quantity) * @as(i256, signed) > 0) {
            next_cost_basis = try dec.add(cost_basis, try dec.mul(signed, fill.price));
        } else if (absLessOrEqual(signed, quantity)) {
            const released_basis: i128 = if (absEqual(signed, quantity))
                cost_basis
            else
                try dec.mulDiv(cost_basis, absValue(signed), absValue(quantity));
            realized_pnl = try dec.add(
                realized_pnl,
                try dec.sub(try dec.mul(-signed, fill.price), released_basis),
            );
            next_cost_basis = try dec.sub(cost_basis, released_basis);
        } else {
            const closing_quantity = -quantity;
            realized_pnl = try dec.add(
                realized_pnl,
                try dec.sub(try dec.mul(-closing_quantity, fill.price), cost_basis),
            );
            next_cost_basis = try dec.mul(next_quantity, fill.price);
        }

        realized_pnl = try dec.sub(realized_pnl, fill.fee);
        const next_cash_flow = try dec.sub(
            try dec.sub(cash_flow, try dec.mul(signed, fill.price)),
            fill.fee,
        );
        if (next_quantity == 0) {
            next_cost_basis = 0;
            realized_pnl = next_cash_flow;
        }
        const next_cash = try dec.sub(
            try dec.sub(self.cash, try dec.mul(signed, fill.price)),
            fill.fee,
        );

        const stored_symbol = if (index) |i| self.ledgers.items[i].symbol else fill.symbol;
        try self.upsertLedger(.{
            .symbol = stored_symbol,
            .quantity = next_quantity,
            .cost_basis = next_cost_basis,
            .cash_flow = next_cash_flow,
            .realized_pnl = realized_pnl,
        });
        self.cash = next_cash;
        try self.fill_ids.append(self.gpa, fill.fill_id);
        return self.position(stored_symbol);
    }

    /// Retire one expired contract ledger at its final settlement value: cash
    /// and cash flow move by ``settlement_value`` and the ledger finalizes
    /// exactly like a closing fill at that value. Retiring an already flat
    /// ledger is refused so settlement effects stay unambiguous.
    pub fn retirePosition(self: *VirtualBook, symbol: []const u8, settlement_value: i128) Error!PositionSnapshot {
        const index = self.ledgerIndex(symbol) orelse return error.UnknownPosition;
        const ledger = self.ledgers.items[index];
        if (ledger.quantity == 0) return error.PositionFlat;
        const next_cash_flow = try dec.add(ledger.cash_flow, settlement_value);
        self.ledgers.items[index] = .{
            .symbol = ledger.symbol,
            .quantity = 0,
            .cost_basis = 0,
            .cash_flow = next_cash_flow,
            .realized_pnl = next_cash_flow,
        };
        self.cash = try dec.add(self.cash, settlement_value);
        return self.position(ledger.symbol);
    }

    /// Create and apply an explicit price marker.
    pub fn mark(self: *VirtualBook, symbol: []const u8, price: i128) Error!PositionMarker {
        if (symbol.len == 0) return error.InvalidArgument;
        if (price <= 0) return error.InvalidArgument;
        if (self.markerIndex(symbol)) |index| {
            self.markers.items[index].price = price;
            return .{ .symbol = self.markers.items[index].symbol, .price = price };
        }
        try self.markers.append(self.gpa, .{ .symbol = symbol, .price = price });
        return .{ .symbol = symbol, .price = price };
    }

    /// Return the current explicit marker for a symbol, if any.
    pub fn marker(self: *const VirtualBook, symbol: []const u8) ?PositionMarker {
        const index = self.markerIndex(symbol) orelse return null;
        return .{
            .symbol = self.markers.items[index].symbol,
            .price = self.markers.items[index].price,
        };
    }

    /// Return the current immutable state for one previously held symbol.
    pub fn position(self: *const VirtualBook, symbol: []const u8) Error!PositionSnapshot {
        const index = self.ledgerIndex(symbol) orelse return error.UnknownPosition;
        return self.positionSnapshot(self.ledgers.items[index]);
    }

    /// Return the current immutable book valuation. Positions are appended to
    /// the caller-owned ``out`` in canonical symbol order; the returned
    /// snapshot borrows ``out.items`` until it changes.
    pub fn snapshot(self: *const VirtualBook, out: *std.ArrayList(PositionSnapshot)) Error!BookSnapshot {
        out.clearRetainingCapacity();
        for (self.ledgers.items) |ledger| {
            try out.append(self.gpa, self.positionSnapshot(ledger));
        }
        return .{
            .book_id = self.book_id,
            .initial_cash = self.initial_cash,
            .cash = self.cash,
            .positions = out.items,
            .fill_count = self.fill_ids.items.len,
        };
    }

    fn positionSnapshot(self: *const VirtualBook, ledger: Ledger) PositionSnapshot {
        return .{
            .symbol = ledger.symbol,
            .quantity = ledger.quantity,
            .cost_basis = ledger.cost_basis,
            .cash_flow = ledger.cash_flow,
            .realized_pnl = ledger.realized_pnl,
            .mark_price = if (self.markerIndex(ledger.symbol)) |index|
                self.markers.items[index].price
            else
                null,
        };
    }

    fn ledgerIndex(self: *const VirtualBook, symbol: []const u8) ?usize {
        for (self.ledgers.items, 0..) |ledger, index| {
            if (std.mem.eql(u8, ledger.symbol, symbol)) return index;
        }
        return null;
    }

    fn markerIndex(self: *const VirtualBook, symbol: []const u8) ?usize {
        for (self.markers.items, 0..) |entry, index| {
            if (std.mem.eql(u8, entry.symbol, symbol)) return index;
        }
        return null;
    }

    fn upsertLedger(self: *VirtualBook, ledger: Ledger) Error!void {
        for (self.ledgers.items, 0..) |existing, index| {
            switch (std.mem.order(u8, ledger.symbol, existing.symbol)) {
                .eq => {
                    self.ledgers.items[index] = ledger;
                    return;
                },
                .lt => {
                    try self.ledgers.insert(self.gpa, index, ledger);
                    return;
                },
                .gt => {},
            }
        }
        try self.ledgers.append(self.gpa, ledger);
    }
};

fn absValue(value: i128) i128 {
    return if (value < 0) -value else value;
}

fn absEqual(a: i128, b: i128) bool {
    return absValue(a) == absValue(b);
}

fn absLessOrEqual(a: i128, b: i128) bool {
    return absValue(a) <= absValue(b);
}

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/portfolio/test_book.py from the
// Python engine, encoded on the pinned scale-18 fixed-point type.
// ---------------------------------------------------------------------------

const testing = std.testing;

fn bookFill(
    fill_id: []const u8,
    side: Side,
    quantity: i128,
    price: i128,
    fee: i128,
    symbol: []const u8,
) Fill {
    return .{
        .fill_id = fill_id,
        .order_id = "order-x",
        .symbol = symbol,
        .side = side,
        .quantity = quantity,
        .price = price,
        .fee = fee,
    };
}

test "average cost partial close reversal and fees are exact" {
    var book = VirtualBook.init(testing.allocator, "alpha", dec.fromInt(10_000));
    defer book.deinit();

    const first = try book.applyFill(bookFill(
        "buy-1",
        .buy,
        dec.fromInt(10),
        dec.fromInt(100),
        dec.one,
        "AAPL",
    ));
    try testing.expectEqual(dec.fromInt(10), first.quantity);
    try testing.expectEqual(dec.fromInt(100), try first.averageCost());
    try testing.expectEqual(-dec.one, first.realized_pnl);
    try testing.expect((try first.unrealizedPnl()) == null);
    try testing.expectEqual(dec.fromInt(8_999), book.cash);
    try testing.expect((try first.totalPnl()) == null);

    _ = try book.applyFill(bookFill(
        "buy-2",
        .buy,
        dec.fromInt(5),
        dec.fromInt(110),
        500_000_000_000_000_000,
        "AAPL",
    ));
    const partial = try book.applyFill(bookFill(
        "sell-part",
        .sell,
        dec.fromInt(6),
        dec.fromInt(120),
        250_000_000_000_000_000,
        "AAPL",
    ));
    try testing.expectEqual(dec.fromInt(9), partial.quantity);
    try testing.expectEqual(dec.fromInt(930), partial.cost_basis);
    // 930 / 9 pins the scale-18 half-even counterpart of Python's
    // 103.3333333333333333333333333 (28 significant digits).
    try testing.expectEqual(@as(i128, 103_333_333_333_333_333_333), try partial.averageCost());
    try testing.expectEqual(@as(i128, 98_250_000_000_000_000_000), partial.realized_pnl);

    const reversed = try book.applyFill(bookFill(
        "sell-through",
        .sell,
        dec.fromInt(12),
        dec.fromInt(90),
        750_000_000_000_000_000,
        "AAPL",
    ));
    try testing.expectEqual(-dec.fromInt(3), reversed.quantity);
    try testing.expectEqual(-dec.fromInt(270), reversed.cost_basis);
    try testing.expectEqual(dec.fromInt(90), try reversed.averageCost());
    try testing.expectEqual(@as(i128, -22_500_000_000_000_000_000), reversed.realized_pnl);
    try testing.expectEqual(@as(i128, 10_247_500_000_000_000_000_000), book.cash);

    const marker = try book.mark("AAPL", dec.fromInt(85));
    const marked = try book.position("AAPL");
    try testing.expectEqual(dec.fromInt(85), marker.price);
    try testing.expectEqual(dec.fromInt(85), marked.mark_price.?);
    try testing.expectEqual(dec.fromInt(15), (try marked.unrealizedPnl()).?);
    try testing.expectEqual(@as(i128, -7_500_000_000_000_000_000), (try marked.totalPnl()).?);

    var positions: std.ArrayList(PositionSnapshot) = .empty;
    defer positions.deinit(testing.allocator);
    const snap = try book.snapshot(&positions);
    try testing.expectEqual(@as(i128, 9_992_500_000_000_000_000_000), (try snap.equity()).?);
    try testing.expectEqual(@as(i128, -7_500_000_000_000_000_000), (try snap.totalPnl()).?);
    const unrealized = (try snap.unrealizedPnl()).?;
    try testing.expectEqual(dec.fromInt(15), unrealized);
    try testing.expectEqual(
        try snap.totalPnl(),
        try dec.add(try snap.realizedPnl(), unrealized),
    );
}

test "flat position retains realized pnl and needs no marker" {
    var book = VirtualBook.init(testing.allocator, "round-trip", dec.fromInt(1_000));
    defer book.deinit();
    _ = try book.applyFill(bookFill("buy", .buy, dec.fromInt(2), dec.fromInt(10), 0, "AAPL"));
    _ = try book.applyFill(bookFill(
        "buy-recurring-average",
        .buy,
        dec.one,
        dec.fromInt(11),
        0,
        "AAPL",
    ));
    _ = try book.applyFill(bookFill(
        "partial-close",
        .sell,
        dec.one,
        dec.fromInt(12),
        0,
        "AAPL",
    ));
    const closed = try book.applyFill(bookFill(
        "sell",
        .sell,
        dec.fromInt(2),
        dec.fromInt(10),
        100_000_000_000_000_000,
        "AAPL",
    ));

    try testing.expect(closed.isFlat());
    try testing.expectEqual(@as(i128, 0), try closed.averageCost());
    try testing.expectEqual(@as(i128, 0), closed.cost_basis);
    try testing.expectEqual(@as(i128, 900_000_000_000_000_000), closed.realized_pnl);
    try testing.expectEqual(@as(i128, 0), (try closed.marketValue()).?);
    try testing.expectEqual(@as(i128, 0), (try closed.unrealizedPnl()).?);
    try testing.expectEqual(@as(i128, 1_000_900_000_000_000_000_000), book.cash);

    var positions: std.ArrayList(PositionSnapshot) = .empty;
    defer positions.deinit(testing.allocator);
    const snap = try book.snapshot(&positions);
    try testing.expectEqual(@as(i128, 1_000_900_000_000_000_000_000), (try snap.equity()).?);
    try testing.expectEqual(@as(i128, 900_000_000_000_000_000), (try snap.totalPnl()).?);
}

test "duplicate fill and invalid marker leave book unchanged" {
    var book = VirtualBook.init(testing.allocator, "alpha", dec.fromInt(100));
    defer book.deinit();
    const applied = bookFill("one", .buy, dec.one, dec.fromInt(10), 0, "AAPL");
    _ = try book.applyFill(applied);

    try testing.expectError(error.DuplicateFill, book.applyFill(applied));
    try testing.expectError(error.InvalidArgument, book.mark("AAPL", 0));

    try testing.expectEqual(dec.fromInt(90), book.cash);
    try testing.expectEqual(@as(usize, 1), book.fill_ids.items.len);
    try testing.expectEqual(@as(usize, 0), book.markers.items.len);
}

test "retire position finalizes expired contracts exactly" {
    var book = VirtualBook.init(testing.allocator, "expiry", dec.fromInt(1_000));
    defer book.deinit();
    _ = try book.applyFill(bookFill(
        "buy-option",
        .buy,
        dec.one,
        dec.fromInt(200),
        0,
        "AAPL240119P00100000",
    ));
    const worthless = try book.retirePosition("AAPL240119P00100000", 0);

    try testing.expect(worthless.isFlat());
    try testing.expectEqual(@as(i128, 0), worthless.cost_basis);
    try testing.expectEqual(@as(i128, -200_000_000_000_000_000_000), worthless.realized_pnl);
    try testing.expectEqual(@as(i128, 0), (try worthless.marketValue()).?);
    try testing.expectEqual(dec.fromInt(800), book.cash);

    var positions: std.ArrayList(PositionSnapshot) = .empty;
    defer positions.deinit(testing.allocator);
    var snap = try book.snapshot(&positions);
    try testing.expectEqual(dec.fromInt(800), (try snap.equity()).?);
    try testing.expectEqual(@as(i128, -200_000_000_000_000_000_000), (try snap.totalPnl()).?);

    var assigned = VirtualBook.init(testing.allocator, "assignment", dec.fromInt(1_000));
    defer assigned.deinit();
    _ = try assigned.applyFill(bookFill(
        "sell-option",
        .sell,
        dec.one,
        dec.fromInt(100),
        0,
        "AAPL240119P00100000",
    ));
    const settled = try assigned.retirePosition("AAPL240119P00100000", -dec.fromInt(250));

    try testing.expectEqual(@as(i128, -150_000_000_000_000_000_000), settled.realized_pnl);
    try testing.expectEqual(dec.fromInt(850), assigned.cash);

    try testing.expectError(error.PositionFlat, book.retirePosition("AAPL240119P00100000", 0));
    try testing.expectError(error.UnknownPosition, book.retirePosition("AAPL", 0));
}

test "snapshot positions are ordered by canonical symbol" {
    var book = VirtualBook.init(testing.allocator, "ordered", dec.fromInt(1_000));
    defer book.deinit();
    _ = try book.applyFill(bookFill(
        "msft",
        .buy,
        dec.one,
        dec.fromInt(10),
        0,
        "MSFT",
    ));
    _ = try book.applyFill(bookFill(
        "aapl",
        .buy,
        dec.one,
        dec.fromInt(10),
        0,
        "AAPL",
    ));
    var positions: std.ArrayList(PositionSnapshot) = .empty;
    defer positions.deinit(testing.allocator);
    const snap = try book.snapshot(&positions);
    try testing.expectEqual(@as(usize, 2), snap.positions.len);
    try testing.expectEqualStrings("AAPL", snap.positions[0].symbol);
    try testing.expectEqualStrings("MSFT", snap.positions[1].symbol);
    try testing.expectEqual(@as(usize, 2), snap.fill_count);
}

test "ten thousand generated fill sequences conserve cash and position" {
    const initial_cash = dec.fromInt(100_000);
    var case_index: usize = 0;
    while (case_index < 10_000) : (case_index += 1) {
        var book = VirtualBook.init(testing.allocator, "generated", initial_cash);
        defer book.deinit();
        var expected_cash: i256 = initial_cash;
        var expected_quantity: i256 = 0;
        var id_buffers: [5][32]u8 = undefined;

        var step: usize = 0;
        while (step < 5) : (step += 1) {
            const buy = ((case_index >> @intCast(step)) + step) % 2 == 0;
            const side: Side = if (buy) .buy else .sell;
            const quantity: i128 =
                @as(i128, @intCast((case_index * (step + 3) + step) % 7 + 1)) * dec.one;
            const price: i128 =
                @divTrunc(@as(i128, @intCast(9000 + (case_index * 37 + step * 101) % 3001)) * dec.one, 100);
            const fee: i128 =
                @divTrunc((@as(i128, @intCast((case_index + step * 3) % 7)) - 2) * dec.one, 100);
            const signed: i128 = if (buy) quantity else -quantity;
            expected_cash -= @divTrunc(@as(i256, signed) * @as(i256, price), dec.one) + @as(i256, fee);
            expected_quantity += signed;

            const fill_id = std.fmt.bufPrint(&id_buffers[step], "{d}-{d}", .{ case_index, step }) catch unreachable;
            _ = try book.applyFill(.{
                .fill_id = fill_id,
                .order_id = "order-generated",
                .symbol = "AAPL",
                .side = side,
                .quantity = quantity,
                .price = price,
                .fee = fee,
            });
        }

        const mark: i128 =
            @divTrunc(@as(i128, @intCast(9500 + case_index % 2001)) * dec.one, 100);
        _ = try book.mark("AAPL", mark);
        const position = try book.position("AAPL");
        var positions: std.ArrayList(PositionSnapshot) = .empty;
        defer positions.deinit(testing.allocator);
        const snap = try book.snapshot(&positions);

        try testing.expect(expected_cash == @as(i256, book.cash));
        try testing.expect(expected_quantity == @as(i256, position.quantity));
        const expected_market_value: i256 = @divTrunc(expected_quantity * @as(i256, mark), dec.one);
        try testing.expectEqual(
            expected_market_value,
            @as(i256, (try snap.marketValue()).?),
        );
        try testing.expectEqual(
            @as(i256, book.cash) + expected_market_value,
            @as(i256, (try snap.equity()).?),
        );
        try testing.expectEqual(
            try snap.totalPnl(),
            try dec.add(try snap.realizedPnl(), (try snap.unrealizedPnl()).?),
        );
        try testing.expectEqual(@as(usize, 5), snap.fill_count);
    }
}
