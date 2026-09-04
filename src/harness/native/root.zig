//! Native core entry module for the quant-harness.
//!
//! The smoke routine stays pure and deterministic so the toolchain gate remains
//! reproducible. ``types`` ports the deterministic core domain (canonical
//! symbols, seeded identifiers, the pinned i128 fixed-point decimal, and the
//! canonical serialization shared with the Python codecs), ``ffi`` defines the
//! stable C ABI consumed by ``src/harness/_native.py``, and ``arrow`` bridges
//! native fixed-point decimals through the Arrow C Data Interface.

const std = @import("std");

pub const types = @import("types.zig");
pub const ffi = @import("ffi.zig");
pub const arrow = @import("arrow.zig");

/// Returns the sum of two i64 values, saturating at the type bounds.
pub fn saturatingAdd(a: i64, b: i64) i64 {
    return a +| b;
}

test "saturating add stays within i64 bounds" {
    try std.testing.expectEqual(@as(i64, 2), saturatingAdd(1, 1));
    try std.testing.expectEqual(std.math.maxInt(i64), saturatingAdd(std.math.maxInt(i64), 1));
    try std.testing.expectEqual(std.math.minInt(i64), saturatingAdd(std.math.minInt(i64), -1));
}

test {
    _ = types;
    _ = ffi;
    _ = arrow;
}

// Force analysis of the C ABI exports so the shared-library artifact always
// contains them, even though the module itself is lazily analyzed.
comptime {
    _ = &ffi.harness_native_abi_version;
    _ = &ffi.harness_symbol_canonicalize;
    _ = &ffi.harness_id_derive;
    _ = &ffi.harness_decimal_text;
    _ = &ffi.harness_trade_row_serialize;
    _ = &ffi.harness_decimal_batch_values;
    _ = &ffi.harness_arrow_decimal_batch;
}
