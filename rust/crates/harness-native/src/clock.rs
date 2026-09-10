//! Deterministic simulation clock, ported from `src/harness/core/clock.py`.
//!
//! The native kernel only ever moves forward from explicit events, so only
//! `SimClock` crosses the port boundary. Timestamps are `i128` epoch
//! nanoseconds, matching the native timestamp convention of [`crate::types`].

/// A deterministic clock that only moves monotonically forward.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SimClock {
    head_ns: i128,
}

/// A checked failure from a `SimClock` mutation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ClockError {
    /// `advance_to` was called with a value older than the current head.
    RetrogradeTimestamp,
    /// `advance_by` was called with a negative duration.
    NegativeDelta,
}

impl core::fmt::Display for ClockError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::RetrogradeTimestamp => {
                "timestamp cannot be older than the SimClock head"
            }
            Self::NegativeDelta => "delta must not be negative",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for ClockError {}

impl SimClock {
    /// Create a clock positioned at `start_ns`.
    pub const fn new(start_ns: i128) -> Self {
        Self { head_ns: start_ns }
    }

    /// Return the simulation head (`SimClock.now`/`head`).
    pub const fn now(&self) -> i128 {
        self.head_ns
    }

    /// Move the head forward, rejecting timestamps older than the head.
    pub fn advance_to(&mut self, value_ns: i128) -> Result<i128, ClockError> {
        if value_ns < self.head_ns {
            return Err(ClockError::RetrogradeTimestamp);
        }
        self.head_ns = value_ns;
        Ok(self.head_ns)
    }

    /// Move the head by a non-negative duration.
    pub fn advance_by(&mut self, delta_ns: i128) -> Result<i128, ClockError> {
        if delta_ns < 0 {
            return Err(ClockError::NegativeDelta);
        }
        self.advance_to(self.head_ns + delta_ns)
    }
}

#[cfg(test)]
mod tests {
    use super::{ClockError, SimClock};

    /// 2024-01-19T14:30:00Z in epoch nanoseconds.
    const STAMP_NS: i128 = 1_705_674_600_000_000_000;

    #[test]
    fn sim_clock_advances_only_from_explicit_monotonic_events() {
        let mut sim = SimClock::new(STAMP_NS);
        assert_eq!(sim.now(), STAMP_NS);

        assert_eq!(sim.advance_by(1_000), Ok(STAMP_NS + 1_000));
        assert_eq!(sim.now(), STAMP_NS + 1_000);

        assert_eq!(
            sim.advance_to(STAMP_NS),
            Err(ClockError::RetrogradeTimestamp)
        );
        assert_eq!(sim.now(), STAMP_NS + 1_000);

        assert_eq!(sim.advance_by(-1_000), Err(ClockError::NegativeDelta));
        assert_eq!(sim.now(), STAMP_NS + 1_000);

        // Advancing to the current head is a no-op.
        assert_eq!(sim.advance_to(STAMP_NS + 1_000), Ok(STAMP_NS + 1_000));
    }
}
