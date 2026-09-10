//! Event-driven tick indicator protocol and shared kernels.
//!
//! Deterministic core of `harness.indicators.base`. Indicators are tick-native:
//! they consume quote and trade events in event time, maintain O(1)-amortized
//! incremental state, and never read a wall clock or recompute over history.
//! Event time floors onto half-open buckets anchored at the Unix epoch, exactly
//! mirroring the kernel's bar derivation, so emission boundaries coincide.
//!
//! The full Python base class also implements end-of-bucket sample emission and
//! session-anchor resets; the staged Rust port provides the arithmetic context,
//! the ordering guard, bucket flooring, the sample record, and the tick
//! protocol. Sample emission lands with the indicator runtime task.

use harness_num::Decimal18;

/// Add two exact decimals.
#[must_use]
pub fn add(left: Decimal18, right: Decimal18) -> Decimal18 {
    left.checked_add(right).unwrap_or(left)
}

/// Subtract two exact decimals.
#[must_use]
pub fn subtract(left: Decimal18, right: Decimal18) -> Decimal18 {
    left.checked_sub(right).unwrap_or(left)
}

/// Multiply two exact decimals.
#[must_use]
pub fn multiply(left: Decimal18, right: Decimal18) -> Decimal18 {
    left.checked_mul(right).unwrap_or(left)
}

/// Divide two exact decimals, returning the numerator on division by zero.
#[must_use]
pub fn divide(left: Decimal18, right: Decimal18) -> Decimal18 {
    left.checked_div(right).unwrap_or(left)
}

/// A one-hundred factor used by percentage indicators.
#[must_use]
pub fn hundred() -> Decimal18 {
    Decimal18::from_unscaled(100_000_000_000_000_000_000).expect("one hundred")
}

/// One indicator configuration or ordering failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IndicatorError {
    /// A parameter was invalid.
    Configuration(String),
    /// An event regressed the accepted kernel ordering.
    Ordering(String),
    /// An unknown indicator name was requested.
    Unknown(String),
}

impl std::fmt::Display for IndicatorError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Configuration(message) | Self::Ordering(message) | Self::Unknown(message) => {
                formatter.write_str(message)
            }
        }
    }
}

impl std::error::Error for IndicatorError {}

/// The event class driving an indicator's value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TickDriver {
    /// Quotes drive the value.
    Quote,
    /// Trades drive the value.
    Trade,
}

/// One quote or trade tick.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TickKind {
    /// A top-of-book quote.
    Quote {
        /// Best bid.
        bid: Decimal18,
        /// Best ask.
        ask: Decimal18,
    },
    /// A trade print.
    Trade {
        /// Print price.
        price: Decimal18,
        /// Print quantity.
        quantity: Decimal18,
    },
}

/// One timestamped tick with its kernel sequence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Tick {
    /// Event time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Kernel sequence.
    pub sequence: u64,
    /// Quote or trade payload.
    pub kind: TickKind,
}

/// One emitted indicator value for a closed half-open bucket.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IndicatorSample {
    /// Indicator name.
    pub name: String,
    /// Instrument symbol.
    pub symbol: String,
    /// Inclusive bucket start in epoch nanoseconds.
    pub start_ns: i64,
    /// Exclusive bucket end in epoch nanoseconds.
    pub end_ns: i64,
    /// Indicator value as of the boundary.
    pub value: Decimal18,
}

/// Floor a timestamp onto the half-open bucket grid anchored at the epoch.
#[must_use]
pub fn bucket_start_ns(timestamp_ns: i64, interval_ns: i64) -> i64 {
    if interval_ns <= 0 {
        return timestamp_ns;
    }
    timestamp_ns.div_euclid(interval_ns) * interval_ns
}

/// A deterministic ordering guard: events must not regress `(timestamp,
/// sequence)`.
#[derive(Clone, Copy, Debug, Default)]
pub struct OrderGuard {
    last: Option<(i64, u64)>,
}

impl OrderGuard {
    /// Accept one tick, rejecting a regression.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Ordering`] when the tick regresses.
    pub fn accept(&mut self, timestamp_ns: i64, sequence: u64) -> Result<(), IndicatorError> {
        if let Some((last_ts, last_sequence)) = self.last {
            if (timestamp_ns, sequence) < (last_ts, last_sequence) {
                return Err(IndicatorError::Ordering(
                    "event regresses the accepted kernel ordering".to_string(),
                ));
            }
        }
        self.last = Some((timestamp_ns, sequence));
        Ok(())
    }
}

/// The tick indicator protocol.
pub trait TickIndicator {
    /// The indicator name.
    fn name(&self) -> &'static str;
    /// The event classes driving the value.
    fn drivers(&self) -> &'static [TickDriver];
    /// Consume one tick.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Ordering`] when ordering regresses.
    fn on_tick(&mut self, tick: &Tick) -> Result<(), IndicatorError>;
    /// The current value, or `None` during warm-up.
    fn value(&self) -> Option<Decimal18>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bucket_start_floors_to_the_grid() {
        assert_eq!(bucket_start_ns(0, 60_000_000_000), 0);
        assert_eq!(bucket_start_ns(59_999_999_999, 60_000_000_000), 0);
        assert_eq!(
            bucket_start_ns(60_000_000_000, 60_000_000_000),
            60_000_000_000
        );
        assert_eq!(
            bucket_start_ns(125_000_000_000, 60_000_000_000),
            120_000_000_000
        );
        // Negative timestamps floor toward negative infinity.
        assert_eq!(bucket_start_ns(-1, 60_000_000_000), -60_000_000_000);
    }

    #[test]
    fn ordering_guard_rejects_regressions() {
        let mut guard = OrderGuard::default();
        assert!(guard.accept(10, 1).is_ok());
        assert!(guard.accept(10, 2).is_ok());
        assert!(guard.accept(9, 5).is_err());
        assert!(guard.accept(10, 1).is_err());
    }
}
