//! Deterministic simulation clock, ported from ``src/harness/core/clock.py``.
//!
//! The native kernel only ever moves forward from explicit events, so only
//! :class:`SimClock` crosses the port boundary. ``WallClock`` and the
//! ``ReadonlyClock`` capability view stay Python-side adapter boundaries by
//! design: the native core has no access to wall time at all.
//!
//! Timestamps are i128 epoch nanoseconds, matching the native timestamp
//! convention of ``types.zig``. Python engine timestamps are timezone-aware
//! UTC datetimes with microsecond precision, so nanosecond granularity is a
//! strict superset and preserves the ordering semantics exactly.

const std = @import("std");

pub const SimClockError = error{
    /// ``advanceTo`` was called with a value older than the current head
    /// (Python raises ``ValueError``: "timestamp cannot be older than the
    /// SimClock head").
    RetrogradeTimestamp,
    /// ``advanceBy`` was called with a negative duration (Python raises
    /// ``ValueError``: "delta must not be negative").
    NegativeDelta,
};

/// A deterministic clock that only moves monotonically forward. Semantics are
/// ported exactly from ``harness.core.clock.SimClock``: the head never moves
/// on its own; only explicit ``advanceTo``/``advanceBy`` (driven by events)
/// move it, and both reject retrograde motion.
pub const SimClock = struct {
    head_ns: i128,

    pub fn init(start_ns: i128) SimClock {
        return .{ .head_ns = start_ns };
    }

    /// Return the simulation head (Python ``SimClock.now``/``head``).
    pub fn now(self: SimClock) i128 {
        return self.head_ns;
    }

    /// Move the head forward, rejecting timestamps older than the head.
    pub fn advanceTo(self: *SimClock, value_ns: i128) SimClockError!i128 {
        if (value_ns < self.head_ns) return error.RetrogradeTimestamp;
        self.head_ns = value_ns;
        return self.head_ns;
    }

    /// Move the head by a non-negative duration.
    pub fn advanceBy(self: *SimClock, delta_ns: i128) SimClockError!i128 {
        if (delta_ns < 0) return error.NegativeDelta;
        return self.advanceTo(self.head_ns + delta_ns);
    }
};

// ---------------------------------------------------------------------------
// Tests. Golden vectors mirror tests/harness/core/test_clock.py: STAMP is
// 2024-01-19T14:30:00Z and every mutation is explicit and monotonic.
// ---------------------------------------------------------------------------

const testing = std.testing;

/// 2024-01-19T14:30:00Z in epoch nanoseconds.
const stamp_ns: i128 = 1_705_674_600_000_000_000;

test "sim clock advances only from explicit monotonic events" {
    var sim = SimClock.init(stamp_ns);
    try testing.expectEqual(stamp_ns, sim.now());

    try testing.expectEqual(stamp_ns + 1_000, try sim.advanceBy(1_000));
    try testing.expectEqual(stamp_ns + 1_000, sim.now());

    try testing.expectError(error.RetrogradeTimestamp, sim.advanceTo(stamp_ns));
    try testing.expectEqual(stamp_ns + 1_000, sim.now());

    try testing.expectError(error.NegativeDelta, sim.advanceBy(-1_000));
    try testing.expectEqual(stamp_ns + 1_000, sim.now());

    // Advancing to the current head is a no-op.
    try testing.expectEqual(stamp_ns + 1_000, try sim.advanceTo(stamp_ns + 1_000));
}
