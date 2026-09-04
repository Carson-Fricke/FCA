//! Stable C ABI surface of the native harness core.
//!
//! Every export returns a small integer error code and writes results through
//! caller-provided buffers, so no allocation happens across the boundary and
//! no panic can escape. Variable-length inputs are passed as pointer + length
//! pairs (not NUL-terminated), and 128-bit values cross the boundary as 16
//! little-endian bytes because ``__int128`` is not portable across C ABIs.

const std = @import("std");
const arrow = @import("arrow.zig");
const types = @import("types.zig");

/// Error codes returned by every export.
pub const code = struct {
    pub const ok: c_int = 0;
    pub const invalid_argument: c_int = 1;
    pub const invalid_symbol: c_int = 2;
    pub const buffer_too_small: c_int = 3;
    pub const invalid_decimal: c_int = 4;
    pub const invalid_timestamp: c_int = 5;
    pub const out_of_range: c_int = 6;
    pub const unknown: c_int = 7;
};

fn errorCode(err: types.Error) c_int {
    return switch (err) {
        error.InvalidArgument => code.invalid_argument,
        error.InvalidSymbol => code.invalid_symbol,
        error.BufferTooSmall => code.buffer_too_small,
        error.InvalidDecimal => code.invalid_decimal,
        error.InvalidTimestamp => code.invalid_timestamp,
        error.OutOfRange => code.out_of_range,
    };
}

fn slice(ptr: ?[*]const u8, len: usize) types.Error![]const u8 {
    if (len == 0) return &[_]u8{};
    if (ptr == null) return error.InvalidArgument;
    return ptr.?[0..len];
}

fn outSlice(ptr: ?[*]u8, cap: usize) types.Error![]u8 {
    if (ptr == null) return error.InvalidArgument;
    return ptr.?[0..cap];
}

/// Version of the stable C ABI. The Python loader pins this value.
pub export fn harness_native_abi_version() u32 {
    return types.abi_version;
}

/// Canonicalize a market or OSI symbol, writing the canonical bytes to
/// ``out_ptr`` and the length to ``out_len``.
pub export fn harness_symbol_canonicalize(
    in_ptr: ?[*]const u8,
    in_len: usize,
    out_ptr: ?[*]u8,
    out_cap: usize,
    out_len: ?*usize,
) c_int {
    const input = slice(in_ptr, in_len) catch |err| return errorCode(err);
    const out = outSlice(out_ptr, out_cap) catch |err| return errorCode(err);
    if (out_len == null) return code.invalid_argument;
    const canonical = types.canonicalSymbol(input, out) catch |err| return errorCode(err);
    out_len.?.* = canonical.value.len;
    return code.ok;
}

/// Derive a deterministic UUID, writing 16 raw bytes to ``out_uuid``.
pub export fn harness_id_derive(
    seed_ptr: ?[*]const u8,
    seed_len: usize,
    kind_ptr: ?[*]const u8,
    kind_len: usize,
    ordinal: u64,
    out_uuid: ?[*]u8,
) c_int {
    const seed = slice(seed_ptr, seed_len) catch |err| return errorCode(err);
    const kind = slice(kind_ptr, kind_len) catch |err| return errorCode(err);
    if (out_uuid == null) return code.invalid_argument;
    types.deterministicId(seed, kind, ordinal, out_uuid.?[0..16]) catch |err| return errorCode(err);
    return code.ok;
}

/// Render an unscaled 128-bit integer carried at ``scale`` as the canonical
/// decimal text (byte-identical to ``str(Decimal(unscaled).scaleb(-scale))``).
pub export fn harness_decimal_text(
    unscaled_le: ?[*]const u8,
    scale: u32,
    out_ptr: ?[*]u8,
    out_cap: usize,
    out_len: ?*usize,
) c_int {
    if (unscaled_le == null) return code.invalid_argument;
    const unscaled = std.mem.readInt(i128, unscaled_le.?[0..16], .little);
    const out = outSlice(out_ptr, out_cap) catch |err| return errorCode(err);
    if (out_len == null) return code.invalid_argument;
    const rendered = types.decimalText(unscaled, scale, out) catch |err| return errorCode(err);
    out_len.?.* = rendered.len;
    return code.ok;
}

/// Serialize a canonical trade row (the codecs' trade row layout) to canonical
/// JSON bytes. Optional strings are passed as null pointers.
pub export fn harness_trade_row_serialize(
    event_id_ptr: ?[*]const u8,
    event_id_len: usize,
    timestamp_ns_le: ?[*]const u8,
    sequence: u64,
    symbol_ptr: ?[*]const u8,
    symbol_len: usize,
    price_unscaled_le: ?[*]const u8,
    quantity_unscaled_le: ?[*]const u8,
    venue_ptr: ?[*]const u8,
    venue_len: usize,
    trade_id_ptr: ?[*]const u8,
    trade_id_len: usize,
    out_ptr: ?[*]u8,
    out_cap: usize,
    out_len: ?*usize,
) c_int {
    if (timestamp_ns_le == null or price_unscaled_le == null or quantity_unscaled_le == null) {
        return code.invalid_argument;
    }
    const event_id = slice(event_id_ptr, event_id_len) catch |err| return errorCode(err);
    const symbol = slice(symbol_ptr, symbol_len) catch |err| return errorCode(err);
    const venue: ?[]const u8 = if (venue_ptr == null) null else slice(venue_ptr, venue_len) catch |err| return errorCode(err);
    const trade_id: ?[]const u8 = if (trade_id_ptr == null) null else slice(trade_id_ptr, trade_id_len) catch |err| return errorCode(err);
    const out = outSlice(out_ptr, out_cap) catch |err| return errorCode(err);
    if (out_len == null) return code.invalid_argument;
    const rendered = types.serializeTradeRow(.{
        .event_id = event_id,
        .timestamp_ns = std.mem.readInt(i128, timestamp_ns_le.?[0..16], .little),
        .sequence = sequence,
        .symbol = symbol,
        .price_unscaled = std.mem.readInt(i128, price_unscaled_le.?[0..16], .little),
        .quantity_unscaled = std.mem.readInt(i128, quantity_unscaled_le.?[0..16], .little),
        .venue = venue,
        .trade_id = trade_id,
    }, out) catch |err| return errorCode(err);
    out_len.?.* = rendered.len;
    return code.ok;
}

/// Write the unscaled values of the pinned Arrow decimal batch as 16
/// little-endian bytes per value. ``cap_values`` bounds the write; the number
/// of written values lands in ``written``. A null pointer with
/// ``cap_values == 0`` reports the batch size only.
pub export fn harness_decimal_batch_values(
    out_ptr: ?[*]u8,
    cap_values: usize,
    written: ?*usize,
) c_int {
    if (written == null) return code.invalid_argument;
    if (cap_values < arrow.batch_len) {
        if (out_ptr == null) {
            if (cap_values == 0) {
                written.?.* = arrow.batch_len;
                return code.ok;
            }
            return code.invalid_argument;
        }
        return code.buffer_too_small;
    }
    const out = outSlice(out_ptr, cap_values * 16) catch |err| return errorCode(err);
    for (arrow.batch_values, 0..) |value, index| {
        std.mem.writeInt(i128, out[index * 16 ..][0..16], value, .little);
    }
    written.?.* = arrow.batch_len;
    return code.ok;
}

/// Export the pinned deterministic decimal128(38, 18) batch through the Arrow
/// C Data Interface: ``out_schema`` and ``out_array`` receive initialized
/// ArrowSchema/ArrowArray structs. The consumer must call ``release`` exactly
/// once on each after importing.
pub export fn harness_arrow_decimal_batch(
    out_schema: ?*arrow.ArrowSchema,
    out_array: ?*arrow.ArrowArray,
) c_int {
    if (out_schema == null or out_array == null) return code.invalid_argument;
    arrow.exportDecimalBatch(out_schema.?, out_array.?) catch |err| return switch (err) {
        error.OutOfMemory => code.unknown,
    };
    return code.ok;
}

test "abi export matches the pinned version" {
    try std.testing.expectEqual(types.abi_version, harness_native_abi_version());
}

test "exports report error codes instead of panicking" {
    var buffer: [64]u8 = undefined;
    var written: usize = 0;
    try std.testing.expectEqual(code.ok, harness_symbol_canonicalize(" aapl ", 6, &buffer, buffer.len, &written));
    try std.testing.expectEqual(@as(usize, 4), written);
    try std.testing.expectEqual(code.invalid_argument, harness_symbol_canonicalize(null, 4, &buffer, buffer.len, &written));
    try std.testing.expectEqual(code.invalid_symbol, harness_symbol_canonicalize("../AAPL", 7, &buffer, buffer.len, &written));
    try std.testing.expectEqual(code.invalid_argument, harness_id_derive("", 0, "k", 1, 0, &buffer));
    try std.testing.expectEqual(code.invalid_argument, harness_decimal_batch_values(null, 1, &written));
    var sizes: usize = 0;
    try std.testing.expectEqual(code.ok, harness_decimal_batch_values(null, 0, &sizes));
    try std.testing.expectEqual(@as(usize, arrow.batch_len), sizes);
    try std.testing.expectEqual(code.buffer_too_small, harness_decimal_batch_values(&buffer, 1, &written));
}
