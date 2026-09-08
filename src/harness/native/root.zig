//! Native core entry module for the quant-harness.
//!
//! The smoke routine stays pure and deterministic so the toolchain gate remains
//! reproducible. ``types`` ports the deterministic core domain (canonical
//! symbols, seeded identifiers, the pinned i128 fixed-point decimal, and the
//! canonical serialization shared with the Python codecs), ``ffi`` defines the
//! stable C ABI consumed by ``src/harness/_native.py``, ``arrow`` bridges
//! native fixed-point decimals through the Arrow C Data Interface, and the
//! kernel modules port the deterministic synchronous event kernel:
//! ``clock`` (SimClock), ``bus`` (event queue/bus), ``oms`` (execution-report
//! state machine), and ``books`` (virtual book accounting). ``quant`` adds
//! float64 batch quant kernels (CRR option pricing with tree greeks and
//! implied-value inversions) behind the same C ABI.

const std = @import("std");

pub const types = @import("types.zig");
pub const ffi = @import("ffi.zig");
pub const arrow = @import("arrow.zig");
pub const clock = @import("clock.zig");
pub const bus = @import("bus.zig");
pub const decimal = @import("decimal.zig");
pub const oms_state = @import("oms_state.zig");
pub const oms_manager = @import("oms_manager.zig");
pub const oms = @import("oms.zig");
pub const books = @import("books.zig");
pub const market = @import("market.zig");
pub const fills = @import("fills.zig");
pub const simple_touch = @import("simple_touch.zig");
pub const latency_size = @import("latency_size.zig");
pub const trade_through = @import("trade_through.zig");
pub const matching = @import("matching.zig");
pub const bars = @import("bars.zig");
pub const arrow_batch = @import("arrow_batch.zig");
pub const replay = @import("replay.zig");
pub const metrics = @import("metrics.zig");
pub const quant = @import("quant.zig");

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
    _ = clock;
    _ = bus;
    _ = decimal;
    _ = oms_state;
    _ = oms_manager;
    _ = oms;
    _ = books;
    _ = market;
    _ = fills;
    _ = simple_touch;
    _ = latency_size;
    _ = trade_through;
    _ = matching;
    _ = @import("matching_test.zig");
    _ = bars;
    _ = arrow_batch;
    _ = replay;
    _ = metrics;
    _ = quant;
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
    _ = &ffi.harness_quant_max_steps;
    _ = &ffi.harness_quant_crr_price_batch;
    _ = &ffi.harness_quant_crr_implied_time_batch;
    _ = &ffi.harness_quant_crr_implied_underlying_batch;
}
