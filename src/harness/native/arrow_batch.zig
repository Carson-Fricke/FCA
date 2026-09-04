//! Arrow C Data Interface batch reader for market replay rows, built on the
//! ``ArrowSchema``/``ArrowArray`` extern structs exported by ``arrow.zig``.
//!
//! The reader borrows producer-owned batches and never touches their
//! ``release`` callbacks: release semantics belong to the producer side of
//! the boundary (pyarrow on the Python side; the in-process test encoder
//! frees its own arena). Supported column formats are the ones the market
//! lake pins: ``tsu:UTC`` (microsecond UTC timestamps), ``l`` (i64
//! sequences), ``u`` (UTF-8 strings), and ``d:38,18`` (the repository's
//! decimal128(38, 18) prices and quantities).
//!
//! ``decodeMarketBatch`` reconstructs validated ``market.MarketEvent`` values
//! from one quote or trade batch. Rows must carry ``event_id``, ``timestamp``,
//! ``sequence``, ``symbol``, and the price/quantity columns; Python-side
//! synthetic fallbacks (``{path}#{row}`` identifiers, sequence fallbacks) stay
//! adapter concerns. Strings in the decoded events borrow the batch buffers.

const std = @import("std");
const arrow = @import("arrow.zig");
const market = @import("market.zig");

pub const Error = error{
    OutOfMemory,
    InvalidArgument,
    InvalidSymbol,
    InvalidDecimal,
    /// Arrow struct layout, format string, or buffer access is malformed.
    InvalidArrow,
    OutOfRange,
};

/// The pinned column kinds of a market replay batch.
pub const ColumnKind = enum { timestamp_us_utc, int64, utf8, decimal_18 };

/// A borrowed, validated view over one Arrow struct array.
pub const Batch = struct {
    schema: *const arrow.ArrowSchema,
    array: *const arrow.ArrowArray,

    pub fn init(schema: *const arrow.ArrowSchema, array: *const arrow.ArrowArray) Error!Batch {
        if (array.length < 0 or array.null_count < 0 or array.offset < 0) return error.InvalidArrow;
        if (schema.n_children != array.n_children) return error.InvalidArrow;
        if (array.n_children > 0 and (array.children == null or schema.children == null)) {
            return error.InvalidArrow;
        }
        return .{ .schema = schema, .array = array };
    }

    pub fn numRows(self: *const Batch) usize {
        return @intCast(self.array.length);
    }

    pub fn numColumns(self: *const Batch) usize {
        return @intCast(self.array.n_children);
    }

    fn child(self: *const Batch, index: usize) Error!*const arrow.ArrowArray {
        if (index >= self.numColumns()) return error.InvalidArgument;
        return self.array.children.?[index] orelse return error.InvalidArrow;
    }

    fn childSchema(self: *const Batch, index: usize) Error!*const arrow.ArrowSchema {
        if (index >= self.numColumns()) return error.InvalidArgument;
        return self.schema.children.?[index] orelse return error.InvalidArrow;
    }

    pub fn columnName(self: *const Batch, index: usize) Error![]const u8 {
        const schema = try self.childSchema(index);
        return std.mem.span(schema.name orelse return error.InvalidArrow);
    }

    pub fn columnKind(self: *const Batch, index: usize) Error!ColumnKind {
        const schema = try self.childSchema(index);
        const format = std.mem.span(schema.format orelse return error.InvalidArrow);
        if (std.mem.eql(u8, format, "tsu:UTC")) return .timestamp_us_utc;
        if (std.mem.eql(u8, format, "l")) return .int64;
        if (std.mem.eql(u8, format, "u")) return .utf8;
        if (std.mem.startsWith(u8, format, "d:")) {
            const comma = std.mem.indexOfScalar(u8, format, ',') orelse return error.InvalidArrow;
            const scale_text = format[comma + 1 ..];
            if (std.mem.indexOfScalar(u8, scale_text, ',') != null) return error.InvalidArrow;
            const scale = std.fmt.parseInt(u32, scale_text, 10) catch return error.InvalidArrow;
            if (scale != 18) return error.InvalidArrow;
            return .decimal_18;
        }
        return error.InvalidArrow;
    }

    /// Return the first column with ``name``, or error.
    pub fn columnIndex(self: *const Batch, name: []const u8) Error!usize {
        var index: usize = 0;
        while (index < self.numColumns()) : (index += 1) {
            if (std.mem.eql(u8, try self.columnName(index), name)) return index;
        }
        return error.InvalidArgument;
    }

    fn optionalColumn(self: *const Batch, name: []const u8) ?usize {
        return self.columnIndex(name) catch null;
    }

    fn absoluteRow(self: *const Batch, row: usize) Error!usize {
        if (row >= self.numRows()) return error.InvalidArgument;
        return @as(usize, @intCast(self.array.offset)) + row;
    }

    fn validityBits(self: *const Batch, index: usize) Error!?[*]const u8 {
        const child_array = try self.child(index);
        if (child_array.null_count == 0) return null;
        const buffers = child_array.buffers orelse return error.InvalidArrow;
        return buffers[0] orelse error.InvalidArrow;
    }

    pub fn isNull(self: *const Batch, index: usize, row: usize) Error!bool {
        const bits = (try self.validityBits(index)) orelse return false;
        const bit: usize = try self.absoluteRow(row);
        return (bits[bit >> 3] >> @intCast(bit & 7)) & 1 == 0;
    }

    fn primitiveBuffer(self: *const Batch, index: usize) Error![*]const u8 {
        const child_array = try self.child(index);
        const buffers = child_array.buffers orelse return error.InvalidArrow;
        return buffers[1] orelse error.InvalidArrow;
    }

    /// Read one non-null decimal128(38, 18) unscaled value.
    pub fn decimalValue(self: *const Batch, index: usize, row: usize) Error!i128 {
        if (try self.isNull(index, row)) return error.InvalidArrow;
        const buffer = try self.primitiveBuffer(index);
        const abs = try self.absoluteRow(row);
        return std.mem.readInt(i128, (buffer + abs * 16)[0..16], .little);
    }

    /// Read one non-null i64 value.
    pub fn int64Value(self: *const Batch, index: usize, row: usize) Error!i64 {
        if (try self.isNull(index, row)) return error.InvalidArrow;
        const buffer = try self.primitiveBuffer(index);
        const abs = try self.absoluteRow(row);
        return std.mem.readInt(i64, (buffer + abs * 8)[0..8], .little);
    }

    /// Read one microsecond UTC timestamp as epoch nanoseconds.
    pub fn timestampNsValue(self: *const Batch, index: usize, row: usize) Error!i128 {
        const micros: i128 = try self.int64Value(index, row);
        return micros * 1_000;
    }

    /// Borrow one non-null UTF-8 string from the batch data buffer.
    pub fn utf8Value(self: *const Batch, index: usize, row: usize) Error![]const u8 {
        if (try self.isNull(index, row)) return error.InvalidArrow;
        const child_array = try self.child(index);
        const buffers = child_array.buffers orelse return error.InvalidArrow;
        const offsets_buffer = buffers[1] orelse return error.InvalidArrow;
        const data = buffers[2] orelse return error.InvalidArrow;
        const abs = try self.absoluteRow(row);
        const start = std.mem.readInt(i32, (offsets_buffer + abs * 4)[0..4], .little);
        const end = std.mem.readInt(i32, (offsets_buffer + (abs + 1) * 4)[0..4], .little);
        if (start < 0 or end < start) return error.InvalidArrow;
        return data[@intCast(start)..@intCast(end)];
    }
};

/// Which market replay schema one batch carries (detected by columns).
pub const MarketBatchKind = enum { quote, trade };

/// Detect the batch kind: quote batches carry ``bid_price``, trade batches
/// carry ``price``.
pub fn detectMarketBatchKind(batch: *const Batch) Error!MarketBatchKind {
    const has_bid = batch.optionalColumn("bid_price") != null;
    const has_price = batch.optionalColumn("price") != null;
    if (has_bid and !has_price) return .quote;
    if (has_price and !has_bid) return .trade;
    return error.InvalidArgument;
}

fn requireKind(batch: *const Batch, index: usize, kind: ColumnKind) Error!void {
    if (try batch.columnKind(index) != kind) return error.InvalidArrow;
}

fn requireDecimalColumn(batch: *const Batch, name: []const u8) Error!usize {
    const index = try batch.columnIndex(name);
    try requireKind(batch, index, .decimal_18);
    return index;
}

fn requireUtf8Column(batch: *const Batch, name: []const u8) Error!usize {
    const index = try batch.columnIndex(name);
    try requireKind(batch, index, .utf8);
    return index;
}

/// Decode one quote or trade batch into validated market events. Strings in
/// the decoded events borrow the batch buffers; ``out`` is caller-owned.
pub fn decodeMarketBatch(
    gpa: std.mem.Allocator,
    schema: *const arrow.ArrowSchema,
    array: *const arrow.ArrowArray,
    out: *std.ArrayList(market.MarketEvent),
) Error!void {
    const batch = try Batch.init(schema, array);
    const kind = try detectMarketBatchKind(&batch);

    const event_id = try requireUtf8Column(&batch, "event_id");
    const timestamp = try batch.columnIndex("timestamp");
    try requireKind(&batch, timestamp, .timestamp_us_utc);
    const sequence = try batch.columnIndex("sequence");
    try requireKind(&batch, sequence, .int64);
    const symbol = try requireUtf8Column(&batch, "symbol");
    const venue = batch.optionalColumn("venue");

    switch (kind) {
        .quote => {
            const bid_price = try requireDecimalColumn(&batch, "bid_price");
            const bid_quantity = try requireDecimalColumn(&batch, "bid_quantity");
            const ask_price = try requireDecimalColumn(&batch, "ask_price");
            const ask_quantity = try requireDecimalColumn(&batch, "ask_quantity");
            var row: usize = 0;
            while (row < batch.numRows()) : (row += 1) {
                const event = market.MarketEvent{ .quote = .{
                    .event_id = try batch.utf8Value(event_id, row),
                    .timestamp_ns = try batch.timestampNsValue(timestamp, row),
                    .sequence = try decodeSequence(&batch, sequence, row),
                    .symbol = try batch.utf8Value(symbol, row),
                    .bid_price = try batch.decimalValue(bid_price, row),
                    .bid_quantity = try batch.decimalValue(bid_quantity, row),
                    .ask_price = try batch.decimalValue(ask_price, row),
                    .ask_quantity = try batch.decimalValue(ask_quantity, row),
                    .venue = try optionalUtf8(&batch, venue, row),
                } };
                _ = try market.QuoteEvent.init(event.quote);
                try out.append(gpa, event);
            }
        },
        .trade => {
            const price = try requireDecimalColumn(&batch, "price");
            const quantity = try requireDecimalColumn(&batch, "quantity");
            const trade_id = batch.optionalColumn("trade_id");
            if (trade_id != null) try requireKind(&batch, trade_id.?, .utf8);
            var row: usize = 0;
            while (row < batch.numRows()) : (row += 1) {
                const event = market.MarketEvent{ .trade = .{
                    .event_id = try batch.utf8Value(event_id, row),
                    .timestamp_ns = try batch.timestampNsValue(timestamp, row),
                    .sequence = try decodeSequence(&batch, sequence, row),
                    .symbol = try batch.utf8Value(symbol, row),
                    .price = try batch.decimalValue(price, row),
                    .quantity = try batch.decimalValue(quantity, row),
                    .venue = try optionalUtf8(&batch, venue, row),
                    .trade_id = try optionalUtf8(&batch, trade_id, row),
                } };
                _ = try market.TradeEvent.init(event.trade);
                try out.append(gpa, event);
            }
        },
    }
}

fn decodeSequence(batch: *const Batch, index: usize, row: usize) Error!u64 {
    const value = try batch.int64Value(index, row);
    if (value < 0) return error.OutOfRange;
    return @intCast(value);
}

fn optionalUtf8(batch: *const Batch, index: ?usize, row: usize) Error!?[]const u8 {
    const column = index orelse return null;
    if (try batch.isNull(column, row)) return null;
    return try batch.utf8Value(column, row);
}

// ---------------------------------------------------------------------------
// Test-side producer: build Arrow struct batches in an arena so the reader
// above can be exercised without pyarrow.
// ---------------------------------------------------------------------------

/// Test-side column spec for ``TestBatch`` (kept public so other native
/// modules can exercise the Arrow bridge without pyarrow).
pub const TestColumn = struct {
    name: []const u8,
    format: []const u8,
    int_values: []const i64 = &.{},
    decimal_values: []const ?i128 = &.{},
    utf8_values: []const ?[]const u8 = &.{},
};

/// In-memory Arrow struct-batch producer used by native tests.
pub const TestBatch = struct {
    arena_state: std.heap.ArenaAllocator,
    schema: arrow.ArrowSchema = undefined,
    array: arrow.ArrowArray = undefined,

    pub fn deinit(self: *TestBatch) void {
        self.arena_state.deinit();
    }

    pub fn build(gpa: std.mem.Allocator, columns: []const TestColumn) Error!TestBatch {
        var self = TestBatch{ .arena_state = std.heap.ArenaAllocator.init(gpa) };
        errdefer self.arena_state.deinit();
        const arena = self.arena_state.allocator();
        const rows = columnRows(columns[0]);
        for (columns) |column| {
            if (columnRows(column) != rows) return error.InvalidArgument;
        }

        const child_schemas = try arena.alloc(arrow.ArrowSchema, columns.len);
        const child_arrays = try arena.alloc(arrow.ArrowArray, columns.len);
        const schema_ptrs = try arena.alloc(?*arrow.ArrowSchema, columns.len);
        const array_ptrs = try arena.alloc(?*arrow.ArrowArray, columns.len);

        for (columns, 0..) |column, index| {
            child_schemas[index] = .{
                .format = (try arena.dupeZ(u8, column.format)).ptr,
                .name = (try arena.dupeZ(u8, column.name)).ptr,
                .metadata = null,
                .flags = 0,
                .n_children = 0,
                .children = null,
                .dictionary = null,
                .release = null,
                .private_data = null,
            };

            var null_count: i64 = 0;
            for (column.decimal_values) |value| {
                if (value == null) null_count += 1;
            }
            for (column.utf8_values) |value| {
                if (value == null) null_count += 1;
            }

            var buffers: std.ArrayList(?[*]const u8) = .empty;
            try buffers.append(arena, try encodeValidity(arena, rows, column, null_count));

            if (column.int_values.len > 0) {
                const data = try arena.alloc(u8, rows * 8);
                for (column.int_values, 0..) |value, row| {
                    std.mem.writeInt(i64, data[row * 8 ..][0..8], value, .little);
                }
                try buffers.append(arena, data.ptr);
            } else if (column.decimal_values.len > 0) {
                const data = try arena.alloc(u8, rows * 16);
                for (column.decimal_values, 0..) |value, row| {
                    if (value) |non_null| {
                        std.mem.writeInt(i128, data[row * 16 ..][0..16], non_null, .little);
                    }
                }
                try buffers.append(arena, data.ptr);
            } else {
                var offsets: std.ArrayList(i32) = .empty;
                var data: std.ArrayList(u8) = .empty;
                try offsets.append(arena, 0);
                for (column.utf8_values) |value| {
                    if (value) |text| {
                        try data.appendSlice(arena, text);
                    }
                    try offsets.append(arena, @intCast(data.items.len));
                }
                try buffers.append(arena, @ptrCast(offsets.items.ptr));
                try buffers.append(arena, data.items.ptr);
            }

            child_arrays[index] = .{
                .length = @intCast(rows),
                .null_count = null_count,
                .offset = 0,
                .n_buffers = @intCast(buffers.items.len - 1),
                .buffers = buffers.items.ptr,
                .n_children = 0,
                .children = null,
                .dictionary = null,
                .release = null,
                .private_data = null,
            };
            schema_ptrs[index] = &child_schemas[index];
            array_ptrs[index] = &child_arrays[index];
        }

        const struct_buffers = try arena.alloc(?[*]const u8, 1);
        struct_buffers[0] = null;
        self.schema = .{
            .format = "+s",
            .name = null,
            .metadata = null,
            .flags = 0,
            .n_children = @intCast(columns.len),
            .children = schema_ptrs.ptr,
            .dictionary = null,
            .release = null,
            .private_data = null,
        };
        self.array = .{
            .length = @intCast(rows),
            .null_count = 0,
            .offset = 0,
            .n_buffers = 1,
            .buffers = struct_buffers.ptr,
            .n_children = @intCast(columns.len),
            .children = array_ptrs.ptr,
            .dictionary = null,
            .release = null,
            .private_data = null,
        };
        return self;
    }

    fn columnRows(column: TestColumn) usize {
        return column.int_values.len + column.decimal_values.len + column.utf8_values.len;
    }

    fn encodeValidity(
        arena: std.mem.Allocator,
        rows: usize,
        column: TestColumn,
        null_count: i64,
    ) Error!?[*]const u8 {
        if (null_count == 0) return null;
        const bytes = try arena.alloc(u8, (rows + 7) / 8);
        @memset(bytes, 0);
        var row: usize = 0;
        while (row < rows) : (row += 1) {
            const valid = if (column.decimal_values.len > 0)
                column.decimal_values[row] != null
            else if (column.utf8_values.len > 0)
                column.utf8_values[row] != null
            else
                true;
            if (valid) bytes[row >> 3] |= @as(u8, 1) << @intCast(row & 7);
        }
        return bytes.ptr;
    }
};

// ---------------------------------------------------------------------------
// Tests. The decoder round-trips encoder-built quote and trade batches,
// including validity bitmaps and nullable venue/trade_id columns.
// ---------------------------------------------------------------------------

const testing = std.testing;
const dec = market.dec;

const stamp_ns: i128 = 1_705_674_600_000_000_000;

fn stampMicros() i64 {
    return @intCast(@divTrunc(stamp_ns, 1_000));
}

fn quoteColumns() [8]TestColumn {
    return .{
        .{ .name = "event_id", .format = "u", .utf8_values = &.{ "q-1", "q-2" } },
        .{ .name = "timestamp", .format = "tsu:UTC", .int_values = &.{ stampMicros(), stampMicros() } },
        .{ .name = "sequence", .format = "l", .int_values = &.{ 2, 4 } },
        .{ .name = "symbol", .format = "u", .utf8_values = &.{ "AAPL", "AAPL" } },
        .{ .name = "bid_price", .format = "d:38,18", .decimal_values = &.{ dec.fromInt(100), dec.fromInt(100) } },
        .{ .name = "bid_quantity", .format = "d:38,18", .decimal_values = &.{ dec.fromInt(10), dec.fromInt(10) } },
        .{ .name = "ask_price", .format = "d:38,18", .decimal_values = &.{ dec.fromInt(101), dec.fromInt(101) } },
        .{ .name = "ask_quantity", .format = "d:38,18", .decimal_values = &.{ dec.fromInt(10), dec.fromInt(10) } },
    };
}

test "batch reader exposes pinned column kinds and values" {
    var encoded = try TestBatch.build(testing.allocator, &quoteColumns());
    defer encoded.deinit();
    const batch = try Batch.init(&encoded.schema, &encoded.array);

    try testing.expectEqual(@as(usize, 2), batch.numRows());
    try testing.expectEqual(@as(usize, 8), batch.numColumns());
    try testing.expectEqual(ColumnKind.utf8, try batch.columnKind(0));
    try testing.expectEqual(ColumnKind.timestamp_us_utc, try batch.columnKind(1));
    try testing.expectEqual(ColumnKind.int64, try batch.columnKind(2));
    try testing.expectEqual(ColumnKind.decimal_18, try batch.columnKind(4));
    try testing.expectEqualStrings("bid_price", try batch.columnName(4));
    try testing.expectEqual(@as(usize, 4), try batch.columnIndex("bid_price"));
    try testing.expectEqual(@as(i128, stamp_ns), try batch.timestampNsValue(1, 0));
    try testing.expectEqual(dec.fromInt(101), try batch.decimalValue(6, 1));
    try testing.expectEqualStrings("q-2", try batch.utf8Value(0, 1));
    try testing.expect(!try batch.isNull(0, 0));
    try testing.expectError(error.InvalidArgument, batch.columnIndex("missing"));
    try testing.expectError(error.InvalidArgument, batch.decimalValue(6, 2));
}

test "decodeMarketBatch reconstructs validated quote and trade events" {
    var encoded_quote = try TestBatch.build(testing.allocator, &quoteColumns());
    defer encoded_quote.deinit();
    var events: std.ArrayList(market.MarketEvent) = .empty;
    defer events.deinit(testing.allocator);
    try decodeMarketBatch(testing.allocator, &encoded_quote.schema, &encoded_quote.array, &events);
    try testing.expectEqual(@as(usize, 2), events.items.len);
    try testing.expectEqualStrings("q-1", events.items[0].quote.event_id);
    try testing.expectEqual(@as(u64, 4), events.items[1].quote.sequence);
    try testing.expectEqual(@as(i128, stamp_ns), events.items[1].quote.timestamp_ns);

    const trade_columns = [_]TestColumn{
        .{ .name = "event_id", .format = "u", .utf8_values = &.{"t-1"} },
        .{ .name = "timestamp", .format = "tsu:UTC", .int_values = &.{stampMicros()} },
        .{ .name = "sequence", .format = "l", .int_values = &.{@as(i64, 7)} },
        .{ .name = "symbol", .format = "u", .utf8_values = &.{"AAPL"} },
        .{ .name = "price", .format = "d:38,18", .decimal_values = &.{dec.fromInt(100)} },
        .{ .name = "quantity", .format = "d:38,18", .decimal_values = &.{dec.fromInt(2)} },
        .{ .name = "venue", .format = "u", .utf8_values = &.{@as(?[]const u8, null)} },
        .{ .name = "trade_id", .format = "u", .utf8_values = &.{@as(?[]const u8, "t-7")} },
    };
    var encoded_trade = try TestBatch.build(testing.allocator, &trade_columns);
    defer encoded_trade.deinit();
    var trades: std.ArrayList(market.MarketEvent) = .empty;
    defer trades.deinit(testing.allocator);
    try decodeMarketBatch(testing.allocator, &encoded_trade.schema, &encoded_trade.array, &trades);
    try testing.expectEqual(@as(usize, 1), trades.items.len);
    try testing.expectEqualStrings("t-1", trades.items[0].trade.event_id);
    try testing.expectEqual(dec.fromInt(100), trades.items[0].trade.price);
    try testing.expect(trades.items[0].trade.venue == null);
    try testing.expectEqualStrings("t-7", trades.items[0].trade.trade_id.?);
}

test "decodeMarketBatch rejects malformed batches" {
    var events: std.ArrayList(market.MarketEvent) = .empty;
    defer events.deinit(testing.allocator);

    // A negative sequence leaves the validated non-negative domain.
    var bad_sequence = quoteColumns();
    bad_sequence[2].int_values = &.{ -1, 4 };
    var encoded_sequence = try TestBatch.build(testing.allocator, &bad_sequence);
    defer encoded_sequence.deinit();
    try testing.expectError(error.OutOfRange, decodeMarketBatch(testing.allocator, &encoded_sequence.schema, &encoded_sequence.array, &events));

    // A wrong decimal scale is refused; only the pinned 38,18 is accepted.
    var bad_scale = quoteColumns();
    bad_scale[4].format = "d:38,9";
    var encoded_scale = try TestBatch.build(testing.allocator, &bad_scale);
    defer encoded_scale.deinit();
    try testing.expectError(error.InvalidArrow, decodeMarketBatch(testing.allocator, &encoded_scale.schema, &encoded_scale.array, &events));

    // A batch carrying neither quote nor trade price columns is ambiguous.
    const ambiguous = quoteColumns()[0..4].* ++ [_]TestColumn{
        .{ .name = "extra", .format = "u", .utf8_values = &.{ "x", "y" } },
        .{ .name = "extra2", .format = "u", .utf8_values = &.{ "x", "y" } },
    };
    var encoded_ambiguous = try TestBatch.build(testing.allocator, &ambiguous);
    defer encoded_ambiguous.deinit();
    try testing.expectError(error.InvalidArgument, decodeMarketBatch(testing.allocator, &encoded_ambiguous.schema, &encoded_ambiguous.array, &events));
}
