//! Core domain types for the native harness core.
//!
//! This module ports the deterministic core from ``src/harness/core`` to Zig:
//! canonical market symbols, seeded deterministic identifiers, the pinned
//! i128 fixed-point decimal, and the canonical (byte-identical) serialization
//! shared with the Python codecs.
//!
//! Determinism rules: no wall-clock, no unseeded random, no network. Every
//! routine is a pure function of its inputs. Errors are returned, never
//! panicked, so no panic can cross the FFI boundary.

const std = @import("std");

/// Version of the stable C ABI exported by the native core. The Python loader
/// (``src/harness/_native.py``) pins the same value and refuses to load a
/// library that disagrees.
///
/// v2 adds the float64 batch quant kernels (``harness_quant_*`` in ``ffi``)
/// alongside the v1 decimal engine exports.
pub const abi_version: u32 = 2;

/// Pinned fixed-point scale for native decimals. This matches the repository's
/// binary decimal convention, ``pa.decimal128(38, 18)`` in
/// ``src/lake/contracts/schemas.py``. The harness codecs additionally pin the
/// canonical *text* encoding to exact decimal strings
/// (``harness.decimal.encoding: string-exact`` in
/// ``src/harness/core/types/schemas.py``); ``decimalText`` reproduces Python's
/// ``str(Decimal)`` form for values carried at this scale.
pub const decimal_scale: u32 = 18;
pub const decimal_precision: u32 = 38;

/// Largest |unscaled| value representable by decimal128(38, 18).
pub const max_unscaled: u128 = pow10(38) - 1;

pub const Error = error{
    InvalidArgument,
    InvalidSymbol,
    InvalidDecimal,
    InvalidTimestamp,
    OutOfRange,
    BufferTooSmall,
};

fn pow10(comptime n: usize) u128 {
    comptime {
        var value: u128 = 1;
        var i: usize = 0;
        while (i < n) : (i += 1) value *= 10;
        return value;
    }
}

fn isAsciiWhitespace(byte: u8) bool {
    return byte == ' ' or byte == '\t' or byte == '\n' or byte == '\r' or
        byte == 0x0b or byte == 0x0c;
}

fn isAsciiDigit(byte: u8) bool {
    return byte >= '0' and byte <= '9';
}

fn isSymbolBody(byte: u8) bool {
    return (byte >= 'A' and byte <= 'Z') or (byte >= '0' and byte <= '9') or
        byte == '.' or byte == '/' or byte == ':' or byte == '_' or byte == '-';
}

fn isOsiRoot(byte: u8) bool {
    return (byte >= 'A' and byte <= 'Z') or (byte >= '0' and byte <= '9') or byte == '.';
}

pub const SymbolKind = enum { market, osi };

/// A canonicalized symbol together with its detected kind.
pub const CanonicalSymbol = struct { value: []const u8, kind: SymbolKind };

fn matchesMarketSymbol(value: []const u8) bool {
    if (value.len == 0) return false;
    if (!(value[0] >= 'A' and value[0] <= 'Z') and !isAsciiDigit(value[0])) return false;
    for (value[1..]) |byte| {
        if (!isSymbolBody(byte)) return false;
    }
    return true;
}

fn matchesOsiSymbol(value: []const u8) bool {
    // ^[A-Z0-9.]{1,6}\d{6}[CP]\d{8}$ — total length 16..=21 depending on the
    // root length. The Python regex backtracks the root, so scan every
    // possible split point instead of consuming the root greedily.
    if (value.len < 16 or value.len > 21) return false;
    var root_len: usize = 1;
    while (root_len <= 6) : (root_len += 1) {
        if (value.len != root_len + 15) continue;
        var valid_prefix = true;
        for (value[0..root_len]) |byte| {
            if (!isOsiRoot(byte)) {
                valid_prefix = false;
                break;
            }
        }
        if (!valid_prefix) continue;
        const rest = value[root_len..];
        var index: usize = 0;
        while (index < 6) : (index += 1) {
            if (!isAsciiDigit(rest[index])) break;
        }
        if (index < 6) continue;
        if (rest[6] != 'C' and rest[6] != 'P') continue;
        index = 7;
        while (index < 15) : (index += 1) {
            if (!isAsciiDigit(rest[index])) break;
        }
        if (index == 15) return true;
    }
    return false;
}

/// Canonicalize a market or OSI symbol exactly like
/// ``harness.core.symbols.canonical_symbol``: strip surrounding whitespace,
/// uppercase ASCII, compact a space-padded OSI root, and validate. The
/// returned slice aliases ``out``.
pub fn canonicalSymbol(input: []const u8, out: []u8) Error!CanonicalSymbol {
    if (out.len < input.len) return error.BufferTooSmall;
    var start: usize = 0;
    var end: usize = input.len;
    while (start < end and isAsciiWhitespace(input[start])) : (start += 1) {}
    while (end > start and isAsciiWhitespace(input[end - 1])) : (end -= 1) {}
    const trimmed = input[start..end];
    if (trimmed.len == 0) return error.InvalidSymbol;
    for (trimmed, 0..) |byte, index| {
        out[index] = if (byte >= 'a' and byte <= 'z') byte - 32 else byte;
    }
    var candidate = out[0..trimmed.len];
    var compacted = false;
    if (candidate.len >= 21 and std.mem.indexOfScalar(u8, candidate[0..6], ' ') != null) {
        var keep: usize = 6;
        while (keep > 0 and candidate[keep - 1] == ' ') : (keep -= 1) {}
        std.mem.copyForwards(u8, candidate[keep..], candidate[6..]);
        candidate = candidate[0 .. keep + (candidate.len - 6)];
        compacted = true;
    }
    if (matchesOsiSymbol(candidate)) return .{ .value = candidate, .kind = .osi };
    // A compacted candidate was an OSI attempt; Python falls back to the
    // market regex on the *original* value, which the space would reject.
    if (compacted) return error.InvalidSymbol;
    if (matchesMarketSymbol(candidate)) return .{ .value = candidate, .kind = .market };
    return error.InvalidSymbol;
}

/// Derive a deterministic UUID (raw 16 bytes) exactly like
/// ``harness.core.ids``: sha256(seed_bytes || 0x00 || kind || 0x00 ||
/// ordinal_be_u16bytes), with the UUID version/variant nibbles pinned to
/// version 5. ``seed`` must already be the encoded seed (``str:``/``int:``
/// prefix applied by ``harness.core.ids._seed_bytes``).
pub fn deterministicId(seed: []const u8, kind: []const u8, ordinal: u64, out: *[16]u8) Error!void {
    if (seed.len == 0 or kind.len == 0) return error.InvalidArgument;
    var hasher = std.crypto.hash.sha2.Sha256.init(.{});
    hasher.update(seed);
    hasher.update(&[_]u8{0});
    hasher.update(kind);
    hasher.update(&[_]u8{0});
    var ordinal_bytes: [16]u8 = [_]u8{0} ** 16;
    std.mem.writeInt(u64, ordinal_bytes[8..16], ordinal, .big);
    hasher.update(&ordinal_bytes);
    var digest: [32]u8 = undefined;
    hasher.final(&digest);
    @memcpy(out, digest[0..16]);
    out[6] = (out[6] & 0x0F) | 0x50;
    out[8] = (out[8] & 0x3F) | 0x80;
}

/// Return the canonical decimal text for a value carried at a fixed scale,
/// byte-identical to Python's ``str(Decimal(unscaled).scaleb(-scale))``.
/// Values must fit decimal128(38, 18) when ``scale`` is the pinned scale.
pub fn decimalText(unscaled: i128, scale: u32, out: []u8) Error![]const u8 {
    if (scale > decimal_precision) return error.InvalidDecimal;
    const negative = unscaled < 0;
    const magnitude: u128 = if (unscaled < 0)
        ~@as(u128, @bitCast(unscaled)) +% 1
    else
        @intCast(unscaled);
    if (magnitude > max_unscaled) return error.InvalidDecimal;

    var digits_buf: [39]u8 = undefined;
    const digits = decimalDigits(magnitude, &digits_buf);
    const exponent: i64 = -@as(i64, scale);
    const ndigits: i64 = @intCast(digits.len);
    const adjusted: i64 = exponent + ndigits - 1;

    var writer = Writer{ .buf = out };
    if (negative) writer.writeByte('-') catch return error.BufferTooSmall;
    if (exponent <= 0 and adjusted >= -6) {
        // Plain notation: digits with the decimal point ``scale`` places from
        // the right, padded with leading zeros after "0." when needed.
        const point: usize = @intCast(ndigits + exponent);
        if (point == 0) {
            writer.writeAll("0.") catch return error.BufferTooSmall;
            writer.writeByteN('0', @intCast(-exponent - ndigits)) catch return error.BufferTooSmall;
            writer.writeAll(digits) catch return error.BufferTooSmall;
        } else if (point >= digits.len) {
            writer.writeAll(digits) catch return error.BufferTooSmall;
            writer.writeByteN('0', point - digits.len) catch return error.BufferTooSmall;
        } else {
            writer.writeAll(digits[0..point]) catch return error.BufferTooSmall;
            writer.writeByte('.') catch return error.BufferTooSmall;
            writer.writeAll(digits[point..]) catch return error.BufferTooSmall;
        }
    } else {
        // Scientific notation: first digit, optional fraction, "E", signed
        // adjusted exponent. Python renders non-negative exponents with an
        // explicit '+'.
        writer.writeByte(digits[0]) catch return error.BufferTooSmall;
        if (digits.len > 1) {
            writer.writeByte('.') catch return error.BufferTooSmall;
            writer.writeAll(digits[1..]) catch return error.BufferTooSmall;
        }
        writer.writeByte('E') catch return error.BufferTooSmall;
        if (adjusted >= 0) writer.writeByte('+') catch return error.BufferTooSmall;
        writer.writeInt(adjusted) catch return error.BufferTooSmall;
    }
    return writer.slice();
}

/// Render the magnitude as decimal digits without leading zeros; zero renders
/// as a single "0".
fn decimalDigits(magnitude: u128, buf: *[39]u8) []const u8 {
    if (magnitude == 0) {
        buf[0] = '0';
        return buf[0..1];
    }
    var value = magnitude;
    var index: usize = buf.len;
    while (value != 0) {
        index -= 1;
        buf[index] = @intCast('0' + @as(u8, @intCast(value % 10)));
        value /= 10;
    }
    return buf[index..];
}

/// Canonical row of a trade event, mirroring the trade row produced by
/// ``harness.core.types.codecs``: compact JSON with keys in the codecs' row
/// order and timestamps rendered with ``datetime.isoformat()`` (the codecs'
/// convention for timestamps inside embedded JSON rows). Decimal fields carry
/// the pinned scale and are rendered with ``decimalText``.
pub const TradeRow = struct {
    event_id: []const u8,
    timestamp_ns: i128,
    sequence: u64,
    symbol: []const u8,
    price_unscaled: i128,
    quantity_unscaled: i128,
    venue: ?[]const u8,
    trade_id: ?[]const u8,
};

/// Serialize a canonical trade row to canonical JSON bytes. Every string must
/// already be canonical (non-empty for identifiers; the symbol must equal its
/// own canonical form), matching the validated domain records the codecs see.
pub fn serializeTradeRow(row: TradeRow, out: []u8) Error![]const u8 {
    if (row.event_id.len == 0) return error.InvalidArgument;
    try requireCanonicalSymbol(row.symbol);
    try requireCanonicalText(row.event_id);
    if (row.venue) |venue| try requireCanonicalText(venue);
    if (row.trade_id) |trade_id| try requireCanonicalText(trade_id);

    var writer = Writer{ .buf = out };
    try writer.writeAll("{\"event_id\":");
    try writeJsonString(&writer, row.event_id);
    try writer.writeAll(",\"timestamp\":");
    var ts_buf: [35]u8 = undefined;
    const timestamp = try isoformatUtc(row.timestamp_ns, &ts_buf);
    try writeJsonString(&writer, timestamp);
    try writer.writeAll(",\"sequence\":");
    try writer.writeUint(row.sequence);
    try writer.writeAll(",\"symbol\":");
    try writeJsonString(&writer, row.symbol);
    try writer.writeAll(",\"price\":");
    var price_buf: [64]u8 = undefined;
    try writeJsonString(&writer, try decimalText(row.price_unscaled, decimal_scale, &price_buf));
    try writer.writeAll(",\"quantity\":");
    var qty_buf: [64]u8 = undefined;
    try writeJsonString(&writer, try decimalText(row.quantity_unscaled, decimal_scale, &qty_buf));
    try writer.writeAll(",\"venue\":");
    if (row.venue) |venue| {
        try writeJsonString(&writer, venue);
    } else {
        try writer.writeAll("null");
    }
    try writer.writeAll(",\"trade_id\":");
    if (row.trade_id) |trade_id| {
        try writeJsonString(&writer, trade_id);
    } else {
        try writer.writeAll("null");
    }
    try writer.writeByte('}');
    return writer.slice();
}

/// Validate that the symbol is already canonical (the codecs always see
/// canonicalized symbols). Validation uses a local buffer; the caller keeps
/// using its own slice.
fn requireCanonicalSymbol(symbol: []const u8) Error!void {
    if (symbol.len == 0) return error.InvalidArgument;
    var buffer: [256]u8 = undefined;
    if (symbol.len > buffer.len) return error.InvalidSymbol;
    const canonical = try canonicalSymbol(symbol, &buffer);
    if (!std.mem.eql(u8, canonical.value, symbol)) return error.InvalidSymbol;
}

fn requireCanonicalText(value: []const u8) Error!void {
    if (value.len == 0) return error.InvalidArgument;
    for (value) |byte| {
        if (byte == 0) return error.InvalidArgument;
    }
}

/// Render an epoch-nanosecond instant as a UTC ``datetime.isoformat()`` string
/// (``YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00``). Nanosecond precision below one
/// microsecond is rejected because the Python domain type is a ``datetime``.
pub fn isoformatUtc(timestamp_ns: i128, out: []u8) Error![]const u8 {
    const nanos_per_micro: i128 = 1_000;
    if (@mod(timestamp_ns, nanos_per_micro) != 0) return error.InvalidTimestamp;
    const micros: i128 = @divTrunc(timestamp_ns, nanos_per_micro);
    const micros_per_day: i128 = 86_400_000_000;
    const days: i128 = @divFloor(micros, micros_per_day);
    const micros_of_day: u64 = @intCast(micros - days * micros_per_day);
    const day_seconds: u32 = @intCast(micros_of_day / 1_000_000);
    const microsecond: u32 = @intCast(micros_of_day % 1_000_000);

    const civil = civilFromDays(days);
    if (civil.year < 1 or civil.year > 9999) return error.InvalidTimestamp;

    var writer = Writer{ .buf = out };
    try writer.writeIntPadded(@intCast(civil.year), 4);
    try writer.writeByte('-');
    try writer.writeIntPadded(@intCast(civil.month), 2);
    try writer.writeByte('-');
    try writer.writeIntPadded(@intCast(civil.day), 2);
    try writer.writeByte('T');
    try writer.writeIntPadded(day_seconds / 3600, 2);
    try writer.writeByte(':');
    try writer.writeIntPadded((day_seconds % 3600) / 60, 2);
    try writer.writeByte(':');
    try writer.writeIntPadded(day_seconds % 60, 2);
    if (microsecond != 0) {
        try writer.writeByte('.');
        try writer.writeIntPadded(microsecond, 6);
    }
    try writer.writeAll("+00:00");
    return writer.slice();
}

const Civil = struct { year: i64, month: u32, day: u32 };

/// Howard Hinnant's civil-from-days algorithm over i128 day counts.
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

/// Checked append-only writer so serialization never allocates and never
/// panics; any overflow surfaces as ``error.BufferTooSmall``.
const Writer = struct {
    buf: []u8,
    index: usize = 0,

    fn slice(self: *const Writer) []const u8 {
        return self.buf[0..self.index];
    }

    fn writeByte(self: *Writer, byte: u8) Error!void {
        if (self.index >= self.buf.len) return error.BufferTooSmall;
        self.buf[self.index] = byte;
        self.index += 1;
    }

    fn writeByteN(self: *Writer, byte: u8, count: usize) Error!void {
        if (self.index + count > self.buf.len) return error.BufferTooSmall;
        @memset(self.buf[self.index .. self.index + count], byte);
        self.index += count;
    }

    fn writeAll(self: *Writer, bytes: []const u8) Error!void {
        if (self.index + bytes.len > self.buf.len) return error.BufferTooSmall;
        @memcpy(self.buf[self.index .. self.index + bytes.len], bytes);
        self.index += bytes.len;
    }

    fn writeInt(self: *Writer, value: i64) Error!void {
        var buffer: [20]u8 = undefined;
        const rendered = std.fmt.bufPrint(&buffer, "{d}", .{value}) catch unreachable;
        try self.writeAll(rendered);
    }

    fn writeUint(self: *Writer, value: u64) Error!void {
        var buffer: [20]u8 = undefined;
        const rendered = std.fmt.bufPrint(&buffer, "{d}", .{value}) catch unreachable;
        try self.writeAll(rendered);
    }

    fn writeIntPadded(self: *Writer, value: u64, width: usize) Error!void {
        var buffer: [20]u8 = undefined;
        const rendered = std.fmt.bufPrint(&buffer, "{d}", .{value}) catch unreachable;
        if (rendered.len > width) return error.BufferTooSmall;
        try self.writeByteN('0', width - rendered.len);
        try self.writeAll(rendered);
    }
};

/// Write a JSON string exactly like ``json.dumps(..., ensure_ascii=True)``:
/// literal U+0020..U+007E, short escapes for control characters, and
/// lowercase ``\\uXXXX`` (with surrogate pairs) for everything else.
fn writeJsonString(writer: *Writer, value: []const u8) Error!void {
    try writer.writeByte('"');
    var index: usize = 0;
    while (index < value.len) {
        const byte = value[index];
        if (byte < 0x80) {
            try writeJsonAscii(writer, byte);
            index += 1;
            continue;
        }
        const codepoint = try decodeUtf8(value, index);
        index += utf8SequenceLen(byte);
        try writeJsonCodepoint(writer, codepoint);
    }
    try writer.writeByte('"');
}

fn writeJsonAscii(writer: *Writer, byte: u8) Error!void {
    switch (byte) {
        '"' => try writer.writeAll("\\\""),
        '\\' => try writer.writeAll("\\\\"),
        0x08 => try writer.writeAll("\\b"),
        0x09 => try writer.writeAll("\\t"),
        0x0A => try writer.writeAll("\\n"),
        0x0C => try writer.writeAll("\\f"),
        0x0D => try writer.writeAll("\\r"),
        else => {
            if (byte >= 0x20 and byte <= 0x7E) {
                try writer.writeByte(byte);
            } else {
                try writeUnicodeEscape(writer, byte);
            }
        },
    }
}

fn writeJsonCodepoint(writer: *Writer, codepoint: u21) Error!void {
    if (codepoint < 0x80) {
        try writeJsonAscii(writer, @intCast(codepoint));
        return;
    }
    if (codepoint < 0x10000) {
        try writeUnicodeEscape(writer, @intCast(codepoint));
        return;
    }
    const offset: u32 = @as(u32, codepoint) - 0x10000;
    try writeUnicodeEscape(writer, @intCast(0xD800 + (offset >> 10)));
    try writeUnicodeEscape(writer, @intCast(0xDC00 + (offset & 0x3FF)));
}

fn writeUnicodeEscape(writer: *Writer, value: u32) Error!void {
    try writer.writeAll("\\u");
    var shift: u5 = 12;
    while (true) {
        const nibble: u4 = @intCast((value >> shift) & 0xF);
        const digit: u8 = if (nibble < 10) '0' + @as(u8, nibble) else 'a' + (@as(u8, nibble) - 10);
        try writer.writeByte(digit);
        if (shift == 0) break;
        shift -= 4;
    }
}

fn utf8SequenceLen(first: u8) usize {
    if (first < 0x80) return 1;
    if (first < 0xE0) return 2;
    if (first < 0xF0) return 3;
    return 4;
}

fn decodeUtf8(bytes: []const u8, start: usize) Error!u21 {
    const first = bytes[start];
    const length = utf8SequenceLen(first);
    if (start + length > bytes.len) return error.InvalidArgument;
    // Reject overlong encodings, surrogate halves, and values above U+10FFFF
    // so every accepted byte sequence decodes to a single scalar value.
    switch (length) {
        2 => if (first < 0xC2) return error.InvalidArgument,
        3 => {
            if (first == 0xE0 and bytes[start + 1] < 0xA0) return error.InvalidArgument;
            if (first == 0xED and bytes[start + 1] >= 0xA0) return error.InvalidArgument;
        },
        4 => {
            if (first == 0xF0 and bytes[start + 1] < 0x90) return error.InvalidArgument;
            if (first == 0xF4 and bytes[start + 1] >= 0x90) return error.InvalidArgument;
            if (first > 0xF4) return error.InvalidArgument;
        },
        else => return error.InvalidArgument,
    }
    const continuation = struct {
        fn check(slice: []const u8) bool {
            for (slice) |byte| {
                if (byte & 0xC0 != 0x80) return false;
            }
            return true;
        }
    };
    if (!continuation.check(bytes[start + 1 .. start + length])) return error.InvalidArgument;
    return switch (length) {
        2 => @intCast(@as(u32, first & 0x1F) << 6 | @as(u32, bytes[start + 1] & 0x3F)),
        3 => @intCast(
            @as(u32, first & 0x0F) << 12 |
                @as(u32, bytes[start + 1] & 0x3F) << 6 |
                @as(u32, bytes[start + 2] & 0x3F),
        ),
        4 => @intCast(
            @as(u32, first & 0x07) << 18 |
                @as(u32, bytes[start + 1] & 0x3F) << 12 |
                @as(u32, bytes[start + 2] & 0x3F) << 6 |
                @as(u32, bytes[start + 3] & 0x3F),
        ),
        else => error.InvalidArgument,
    };
}

// ---------------------------------------------------------------------------
// Tests. Expected values are pinned from the Python implementation:
// deterministic IDs from harness.core.ids, decimal text from
// str(Decimal(...)), and record bytes from harness.core.types.codecs.
// ---------------------------------------------------------------------------

const testing = std.testing;

test "abi version is pinned" {
    try testing.expectEqual(@as(u32, 2), abi_version);
}

test "canonical symbols match the Python canonicalization" {
    var buffer: [256]u8 = undefined;

    const lower = try canonicalSymbol(" aapl ", &buffer);
    try testing.expectEqualStrings("AAPL", lower.value);
    try testing.expectEqual(SymbolKind.market, lower.kind);

    const dotted = try canonicalSymbol("BRK.B", &buffer);
    try testing.expectEqualStrings("BRK.B", dotted.value);

    const unpadded = try canonicalSymbol("AAPL240119C00150000", &buffer);
    try testing.expectEqualStrings("AAPL240119C00150000", unpadded.value);
    try testing.expectEqual(SymbolKind.osi, unpadded.kind);

    const padded = try canonicalSymbol("AAPL  240119C00150000", &buffer);
    try testing.expectEqualStrings("AAPL240119C00150000", padded.value);
    try testing.expectEqual(SymbolKind.osi, padded.kind);

    try testing.expectError(error.InvalidSymbol, canonicalSymbol("../AAPL", &buffer));
    try testing.expectError(error.InvalidSymbol, canonicalSymbol("   ", &buffer));
    try testing.expectError(error.BufferTooSmall, canonicalSymbol("AAPL", buffer[0..3]));
}

test "deterministic ids match the Python derivation" {
    const cases = [_]struct {
        seed: []const u8,
        kind: []const u8,
        ordinal: u64,
        hex: []const u8,
    }{
        .{ .seed = "str:abc", .kind = "engine", .ordinal = 0, .hex = "5e07f8554e5454c59b2e82a1c23e8715" },
        .{ .seed = "str:abc", .kind = "event", .ordinal = 1, .hex = "5f4bc7e603ae59d7b9c9d8629073e430" },
        .{ .seed = "str:abc", .kind = "order", .ordinal = 7, .hex = "ca17e7b50d2b5a1589208240bda7da65" },
        .{ .seed = "str:abc", .kind = "fill", .ordinal = 123456789012345, .hex = "0ee8754800535aac8f01b962ae39ef7d" },
        .{ .seed = "int:42", .kind = "engine", .ordinal = 0, .hex = "13a861e80e2c570cade52b8498edf905" },
        .{ .seed = "XY", .kind = "engine", .ordinal = 3, .hex = "617fa2b4386d5472b63a760a3bae48bb" },
    };
    for (cases) |case| {
        var id: [16]u8 = undefined;
        try deterministicId(case.seed, case.kind, case.ordinal, &id);
        var expected: [16]u8 = undefined;
        _ = std.fmt.hexToBytes(&expected, case.hex) catch unreachable;
        try testing.expectEqualSlices(u8, &expected, &id);
    }
    var sink: [16]u8 = undefined;
    try testing.expectError(error.InvalidArgument, deterministicId("", "engine", 0, &sink));
    try testing.expectError(error.InvalidArgument, deterministicId("seed", "", 0, &sink));
}

test "decimal text matches Python str(Decimal) at the pinned scale" {
    const cases = [_]struct { unscaled: i128, text: []const u8 }{
        .{ .unscaled = 1_500_000_000_000_000_000, .text = "1.500000000000000000" },
        .{ .unscaled = 1, .text = "1E-18" },
        .{ .unscaled = 0, .text = "0E-18" },
        .{ .unscaled = -1, .text = "-1E-18" },
        .{ .unscaled = 99_999_999_999_999_999_999, .text = "99.999999999999999999" },
        .{ .unscaled = -1_502_500_000_000_000_000, .text = "-1.502500000000000000" },
        .{ .unscaled = 12_345_678_901_234_567_890, .text = "12.345678901234567890" },
        .{ .unscaled = -99_999_999_999_999_999_999, .text = "-99.999999999999999999" },
        .{ .unscaled = 15_025, .text = "1.5025E-14" },
        .{ .unscaled = 1_000_000_000_000_000_000, .text = "1.000000000000000000" },
        .{ .unscaled = 1_208_925_819_614_629_174_706_176, .text = "1208925.819614629174706176" },
    };
    var buffer: [64]u8 = undefined;
    for (cases) |case| {
        const rendered = try decimalText(case.unscaled, decimal_scale, &buffer);
        try testing.expectEqualStrings(case.text, rendered);
    }
    try testing.expectError(error.InvalidDecimal, decimalText(max_unscaled + 1, decimal_scale, &buffer));
}

test "isoformat matches Python datetime.isoformat for UTC instants" {
    var buffer: [40]u8 = undefined;
    const with_fraction = try isoformatUtc(1_704_164_645_123_456_000, &buffer);
    try testing.expectEqualStrings("2024-01-02T03:04:05.123456+00:00", with_fraction);
    const whole_second = try isoformatUtc(1_704_164_645_000_000_000, &buffer);
    try testing.expectEqualStrings("2024-01-02T03:04:05+00:00", whole_second);
    try testing.expectError(error.InvalidTimestamp, isoformatUtc(1_704_164_645_000_000_001, &buffer));
}

test "trade rows serialize byte-identically to the Python codecs row" {
    const first = "{\"event_id\":\"evt-1\",\"timestamp\":\"2024-01-02T03:04:05.123456+00:00\",\"sequence\":9,\"symbol\":\"AAPL\",\"price\":\"150.250000000000000000\",\"quantity\":\"10.000000000000000000\",\"venue\":\"XNAS\",\"trade_id\":\"t-7\"}";
    const second = "{\"event_id\":\"evt-2\",\"timestamp\":\"2024-01-02T03:04:05.123456+00:00\",\"sequence\":0,\"symbol\":\"AAPL240119C00150000\",\"price\":\"1E-18\",\"quantity\":\"99.999999999999999999\",\"venue\":null,\"trade_id\":null}";
    const third = "{\"event_id\":\"evt-3\",\"timestamp\":\"2024-01-02T03:04:05+00:00\",\"sequence\":0,\"symbol\":\"BRK.B\",\"price\":\"150.250000000000000000\",\"quantity\":\"10.000000000000000000\",\"venue\":\"XNAS\",\"trade_id\":null}";

    var buffer: [512]u8 = undefined;
    const rendered_first = try serializeTradeRow(.{
        .event_id = "evt-1",
        .timestamp_ns = 1_704_164_645_123_456_000,
        .sequence = 9,
        .symbol = "AAPL",
        .price_unscaled = 150_250_000_000_000_000_000,
        .quantity_unscaled = 10_000_000_000_000_000_000,
        .venue = "XNAS",
        .trade_id = "t-7",
    }, &buffer);
    try testing.expectEqualStrings(first, rendered_first);

    const rendered_second = try serializeTradeRow(.{
        .event_id = "evt-2",
        .timestamp_ns = 1_704_164_645_123_456_000,
        .sequence = 0,
        .symbol = "AAPL240119C00150000",
        .price_unscaled = 1,
        .quantity_unscaled = 99_999_999_999_999_999_999,
        .venue = null,
        .trade_id = null,
    }, &buffer);
    try testing.expectEqualStrings(second, rendered_second);

    const rendered_third = try serializeTradeRow(.{
        .event_id = "evt-3",
        .timestamp_ns = 1_704_164_645_000_000_000,
        .sequence = 0,
        .symbol = "BRK.B",
        .price_unscaled = 150_250_000_000_000_000_000,
        .quantity_unscaled = 10_000_000_000_000_000_000,
        .venue = "XNAS",
        .trade_id = null,
    }, &buffer);
    try testing.expectEqualStrings(third, rendered_third);

    try testing.expectError(error.InvalidSymbol, serializeTradeRow(.{
        .event_id = "evt-4",
        .timestamp_ns = 0,
        .sequence = 0,
        .symbol = "aapl",
        .price_unscaled = 0,
        .quantity_unscaled = 0,
        .venue = null,
        .trade_id = null,
    }, &buffer));
}

test "json strings escape exactly like json.dumps with ensure_ascii" {
    var buffer: [128]u8 = undefined;
    var writer = Writer{ .buf = &buffer };
    try writeJsonString(&writer, "plain");
    try testing.expectEqualStrings("\"plain\"", writer.slice());

    writer = .{ .buf = &buffer };
    try writeJsonString(&writer, "a\"b\\c\nd\x01\x7f");
    try testing.expectEqualStrings("\"a\\\"b\\\\c\\nd\\u0001\\u007f\"", writer.slice());

    writer = .{ .buf = &buffer };
    try writeJsonString(&writer, "\u{00e9}\u{2028}\u{1f600}");
    try testing.expectEqualStrings("\"\\u00e9\\u2028\\ud83d\\ude00\"", writer.slice());
}
