//! Arrow C Data Interface bridge for the native harness core.
//!
//! Implements the Arrow C struct definitions (ArrowSchema/ArrowArray) per the
//! stable ``arrow/c/abi.h`` layout and exports a deterministic batch of
//! fixed-point decimals at the pinned scale so Python can consume it through
//! pyarrow's documented ctypes import path (``pa.Array._import_from_c``).
//!
//! Memory ownership follows the Arrow C Data Interface contract: the producer
//! allocates the schema metadata and value buffers, and the consumer releases
//! both structs exactly once by invoking their ``release`` callbacks, which
//! free everything the producer allocated. No panics cross the boundary; the
//! export reports ``error.OutOfMemory`` instead.

const std = @import("std");
const types = @import("types.zig");

/// The Arrow C Data Interface schema descriptor (arrow/c/abi.h layout).
pub const ArrowSchema = extern struct {
    format: ?[*:0]const u8,
    name: ?[*:0]const u8,
    metadata: ?[*]const u8,
    flags: i64,
    n_children: i64,
    children: ?[*]?*ArrowSchema,
    dictionary: ?*ArrowSchema,
    release: ?*const fn (*ArrowSchema) callconv(.c) void,
    private_data: ?*anyopaque,
};

/// The Arrow C Data Interface array descriptor (arrow/c/abi.h layout).
pub const ArrowArray = extern struct {
    length: i64,
    null_count: i64,
    offset: i64,
    n_buffers: i64,
    n_children: i64,
    buffers: ?[*]const ?[*]const u8,
    children: ?[*]?*ArrowArray,
    dictionary: ?*ArrowArray,
    release: ?*const fn (*ArrowArray) callconv(.c) void,
    private_data: ?*anyopaque,
};

/// The pinned Arrow type of the exported batch: decimal128 at the repository's
/// binary decimal precision and scale.
pub const batch_format: [*:0]const u8 = "d:38,18";
pub const batch_name: [*:0]const u8 = "value";
pub const batch_len: usize = 8;

/// The pinned deterministic batch of unscaled decimal128(38, 18) values. The
/// Python bridge test mirrors these values (it reads them back through
/// ``harness_decimal_batch_values``) and asserts the imported pyarrow values.
pub const batch_values: [batch_len]i128 = .{
    1_500_000_000_000_000_000, // 1.500000000000000000
    1, // 0.000000000000000001
    0, // 0
    -1_502_500_000_000_000_000, // -1.502500000000000000
    99_999_999_999_999_999_999, // 99.999999999999999999
    -99_999_999_999_999_999_999, // -99.999999999999999999
    12_345_678_901_234_567_890, // 12.345678901234567890
    15_025, // 0.000000000000015025
};

const c_allocator = std.heap.c_allocator;

const SchemaPrivate = struct {
    release_called: bool = false,
};

const ArrayPrivate = struct {
    raw: []align(1) u8,
    values: [*]const u8,
    buffers: [2]?[*]const u8,
    release_called: bool = false,
};

/// Fill ``out_schema`` and ``out_array`` with the pinned decimal batch. The
/// value buffer is allocated with 16-byte alignment (``i128``); both structs
/// are only written once every allocation has succeeded.
pub fn exportDecimalBatch(out_schema: *ArrowSchema, out_array: *ArrowArray) error{OutOfMemory}!void {
    const schema_private = try c_allocator.create(SchemaPrivate);
    errdefer c_allocator.destroy(schema_private);
    schema_private.* = .{};

    const raw = try c_allocator.alignedAlloc(u8, .of(i128), batch_len * @sizeOf(i128));
    errdefer c_allocator.free(raw);
    for (batch_values, 0..) |value, index| {
        std.mem.writeInt(i128, raw[index * @sizeOf(i128) ..][0..16], value, .little);
    }
    const array_private = try c_allocator.create(ArrayPrivate);
    errdefer c_allocator.destroy(array_private);
    array_private.* = .{
        .raw = raw,
        .values = raw.ptr,
        .buffers = .{ null, raw.ptr },
    };

    out_schema.* = .{
        .format = batch_format,
        .name = batch_name,
        .metadata = null,
        .flags = 0,
        .n_children = 0,
        .children = null,
        .dictionary = null,
        .release = releaseSchema,
        .private_data = schema_private,
    };

    out_array.* = .{
        .length = @intCast(batch_len),
        .null_count = 0,
        .offset = 0,
        .n_buffers = 2,
        .n_children = 0,
        .buffers = &array_private.buffers,
        .children = null,
        .dictionary = null,
        .release = releaseArray,
        .private_data = array_private,
    };
}

/// Free everything the producer allocated for the schema. Idempotent per the
/// Arrow contract: the callback clears itself before freeing.
fn releaseSchema(schema: *ArrowSchema) callconv(.c) void {
    const private: *SchemaPrivate = @ptrCast(@alignCast(schema.private_data orelse return));
    if (private.release_called) return;
    private.release_called = true;
    c_allocator.destroy(private);
    schema.release = null;
    schema.private_data = null;
}

/// Free the value buffer and the array's private data. Idempotent per the
/// Arrow contract: the callback clears itself before freeing.
fn releaseArray(array: *ArrowArray) callconv(.c) void {
    const private: *ArrayPrivate = @ptrCast(@alignCast(array.private_data orelse return));
    if (private.release_called) return;
    private.release_called = true;
    c_allocator.free(private.raw);
    c_allocator.destroy(private);
    array.release = null;
    array.private_data = null;
}

test "exported batch carries the pinned schema and values" {
    var schema: ArrowSchema = undefined;
    var array: ArrowArray = undefined;
    try exportDecimalBatch(&schema, &array);

    defer {
        if (schema.release) |release| release(&schema);
        if (array.release) |release| release(&array);
    }

    try testing.expectEqualStrings("d:38,18", std.mem.span(schema.format.?));
    try testing.expectEqual(@as(i64, @intCast(batch_len)), array.length);
    try testing.expectEqual(@as(i64, 0), array.null_count);
    try testing.expectEqual(@as(i64, 2), array.n_buffers);
    try testing.expectEqual(@as(i64, 0), array.n_children);
    try testing.expect(schema.release != null);
    try testing.expect(array.release != null);

    const buffers = array.buffers.?;
    try testing.expect(buffers[0] == null);
    for (batch_values, 0..) |value, index| {
        const stored = std.mem.readInt(i128, buffers[1].?[index * 16 ..][0..16], .little);
        try testing.expectEqual(value, stored);
    }
}

test "release callbacks are idempotent" {
    var schema: ArrowSchema = undefined;
    var array: ArrowArray = undefined;
    try exportDecimalBatch(&schema, &array);

    const schema_release = schema.release.?;
    const array_release = array.release.?;
    schema_release(&schema);
    array_release(&array);
    schema_release(&schema);
    array_release(&array);

    try testing.expect(schema.release == null);
    try testing.expect(array.release == null);
}

const testing = std.testing;
