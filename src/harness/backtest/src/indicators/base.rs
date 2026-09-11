//! Event-driven tick indicator protocol and the shared state machine.
//!
//! Deterministic core of `harness.indicators.base`. Indicators are tick-native:
//! they consume quote and trade events in event time, maintain O(1)-amortized
//! incremental state, and never read a wall clock or recompute over history.
//! Event time floors onto half-open buckets anchored at the Unix epoch, exactly
//! mirroring the kernel's bar derivation, so emission boundaries coincide.
//!
//! [`IndicatorMachine`] is the shared state machine from the Python base class:
//! it closes and emits the previous half-open bucket when an event rotates into
//! a later one, resets cumulative state on session rollover, skips buckets with
//! no driver event, and supports an end-of-replay [`IndicatorMachine::flush`].
//! The per-indicator arithmetic lives behind the [`IndicatorKernel`] hook trait,
//! so every built-in shares exactly one emission and session contract.

use std::collections::VecDeque;

use num::Decimal18;

/// Nanoseconds in one UTC day.
pub const DAY_NS: i64 = 86_400_000_000_000;

/// Default emission bucket: one minute.
pub const DEFAULT_BUCKET_NS: i64 = 60_000_000_000;

/// Midnight UTC as a nanosecond offset from the start of the UTC day.
pub const MIDNIGHT_UTC_NS: i64 = 0;

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

/// Correctly rounded floor square root of a non-negative scale-18 decimal.
///
/// The fixed-point result is the floor of the exact square root, so it is
/// deterministic and free of floating-point drift. Values whose intermediate
/// would exceed `u128` fall back to the IEEE-754 square root.
#[must_use]
pub fn sqrt(value: Decimal18) -> Decimal18 {
    if !value.is_positive() {
        return Decimal18::ZERO;
    }
    let unscaled = value.unscaled() as u128;
    match unscaled.checked_mul(1_000_000_000_000_000_000u128) {
        Some(scaled) => match Decimal18::from_unscaled(isqrt_u128(scaled) as i128) {
            Ok(root) => root,
            Err(_) => Decimal18::from_f64(value.to_f64().sqrt()).unwrap_or(Decimal18::ZERO),
        },
        None => Decimal18::from_f64(value.to_f64().sqrt()).unwrap_or(Decimal18::ZERO),
    }
}

fn isqrt_u128(value: u128) -> u128 {
    if value < 2 {
        return value;
    }
    let mut low = 0u128;
    let mut high = value;
    while low <= high {
        let middle = low + (high - low) / 2;
        match middle.checked_mul(middle) {
            Some(square) if square <= value => low = middle + 1,
            _ => {
                if middle == 0 {
                    break;
                }
                high = middle - 1;
            }
        }
    }
    high
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

impl TickKind {
    /// The event class of this tick.
    #[must_use]
    pub const fn driver(&self) -> TickDriver {
        match self {
            Self::Quote { .. } => TickDriver::Quote,
            Self::Trade { .. } => TickDriver::Trade,
        }
    }
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

/// Return the UTC session start for `timestamp_ns` at `anchor_ns` days.
#[must_use]
pub fn session_start_ns(timestamp_ns: i64, anchor_ns: i64) -> i64 {
    let day = timestamp_ns.div_euclid(DAY_NS) * DAY_NS;
    let start = day + anchor_ns;
    if timestamp_ns < start {
        start - DAY_NS
    } else {
        start
    }
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

/// The per-indicator incremental arithmetic hooks driven by the state machine.
///
/// Implementations fold accepted events into O(1)-amortized state and expose
/// the current value. Bucket and session hooks let windowed kernels expire or
/// reset state on the emission boundaries the machine computes.
pub trait IndicatorKernel {
    /// The indicator name.
    fn name(&self) -> &'static str;
    /// The event classes consumed by this indicator.
    fn consumes(&self) -> &'static [TickDriver] {
        &[TickDriver::Quote, TickDriver::Trade]
    }
    /// The event classes whose events count toward emission and warm-up.
    fn drivers(&self) -> &'static [TickDriver];
    /// Fold one accepted trade.
    fn on_trade(&mut self, _price: Decimal18, _quantity: Decimal18, _timestamp_ns: i64) {}
    /// Fold one accepted quote.
    fn on_quote(&mut self, _bid: Decimal18, _ask: Decimal18, _timestamp_ns: i64) {}
    /// Reset per-bucket accumulators when a new bucket opens.
    fn on_bucket_open(&mut self, _start_ns: i64) {}
    /// Expire or aggregate state against a closing bucket boundary.
    fn on_bucket_close(&mut self, _end_ns: i64) {}
    /// Reset per-session cumulative state when a session rolls over.
    fn reset_session(&mut self) {}
    /// The current value, or `None` during warm-up.
    fn value(&self) -> Option<Decimal18>;
}

/// The tick indicator protocol exposed to the registry and consumers.
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
    /// Every sample emitted so far, in emission order.
    fn samples(&self) -> &[IndicatorSample] {
        &[]
    }
    /// Close the open bucket, emitting its sample when one is defined.
    fn flush(&mut self) {}
}

/// One symbol-bound indicator: an [`IndicatorKernel`] plus the shared bucket and
/// session emission machine.
#[derive(Clone, Debug)]
pub struct IndicatorMachine<K: IndicatorKernel> {
    kernel: K,
    symbol: String,
    interval_ns: i64,
    session_anchor_ns: Option<i64>,
    samples: Vec<IndicatorSample>,
    open_bucket_ns: Option<i64>,
    bucket_driver_count: u64,
    last_key: Option<(i64, u64)>,
    session_start_ns: Option<i64>,
}

impl<K: IndicatorKernel> IndicatorMachine<K> {
    /// Bind a kernel to one symbol and emission cadence.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] when `interval_ns` is not
    /// positive.
    pub fn with_kernel(
        kernel: K,
        symbol: impl Into<String>,
        interval_ns: i64,
        session_anchor_ns: Option<i64>,
    ) -> Result<Self, IndicatorError> {
        if interval_ns <= 0 {
            return Err(IndicatorError::Configuration(
                "bucket interval must be positive".to_string(),
            ));
        }
        Ok(Self {
            kernel,
            symbol: symbol.into().to_uppercase(),
            interval_ns,
            session_anchor_ns,
            samples: Vec::new(),
            open_bucket_ns: None,
            bucket_driver_count: 0,
            last_key: None,
            session_start_ns: None,
        })
    }

    /// The canonical symbol this machine is bound to.
    #[must_use]
    pub fn symbol(&self) -> &str {
        &self.symbol
    }

    /// The exact emission-bucket interval in nanoseconds.
    #[must_use]
    pub const fn interval_ns(&self) -> i64 {
        self.interval_ns
    }

    /// The kernel driving this machine.
    #[must_use]
    pub const fn kernel(&self) -> &K {
        &self.kernel
    }

    /// The mutable kernel driving this machine.
    pub fn kernel_mut(&mut self) -> &mut K {
        &mut self.kernel
    }

    fn close_open_bucket(&mut self) {
        let Some(bucket) = self.open_bucket_ns.take() else {
            return;
        };
        let end = bucket + self.interval_ns;
        self.kernel.on_bucket_close(end);
        if self.bucket_driver_count > 0 {
            if let Some(value) = self.kernel.value() {
                self.samples.push(IndicatorSample {
                    name: self.kernel.name().to_string(),
                    symbol: self.symbol.clone(),
                    start_ns: bucket,
                    end_ns: end,
                    value,
                });
            }
        }
        self.bucket_driver_count = 0;
    }
}

impl<K: IndicatorKernel> TickIndicator for IndicatorMachine<K> {
    fn name(&self) -> &'static str {
        self.kernel.name()
    }

    fn drivers(&self) -> &'static [TickDriver] {
        self.kernel.drivers()
    }

    fn on_tick(&mut self, tick: &Tick) -> Result<(), IndicatorError> {
        let key = (tick.timestamp_ns, tick.sequence);
        if let Some(last) = self.last_key {
            if key < last {
                return Err(IndicatorError::Ordering(
                    "event regresses the accepted kernel ordering".to_string(),
                ));
            }
        }
        let driver = tick.kind.driver();
        if !self.kernel.consumes().contains(&driver) {
            return Ok(());
        }
        let bucket = bucket_start_ns(tick.timestamp_ns, self.interval_ns);
        if self.open_bucket_ns.is_some_and(|open| bucket > open) {
            self.close_open_bucket();
        }
        if let Some(anchor) = self.session_anchor_ns {
            let session = session_start_ns(tick.timestamp_ns, anchor);
            match self.session_start_ns {
                None => self.session_start_ns = Some(session),
                Some(current) if session > current => {
                    if self.open_bucket_ns.is_some() {
                        self.close_open_bucket();
                    }
                    self.kernel.reset_session();
                    self.session_start_ns = Some(session);
                }
                _ => {}
            }
        }
        if self.open_bucket_ns.is_none() {
            self.open_bucket_ns = Some(bucket);
            self.bucket_driver_count = 0;
            self.kernel.on_bucket_open(bucket);
        }
        match &tick.kind {
            TickKind::Trade { price, quantity } => {
                self.kernel.on_trade(*price, *quantity, tick.timestamp_ns);
            }
            TickKind::Quote { bid, ask } => {
                self.kernel.on_quote(*bid, *ask, tick.timestamp_ns);
            }
        }
        if self.kernel.drivers().contains(&driver) {
            self.bucket_driver_count += 1;
        }
        self.last_key = Some(key);
        Ok(())
    }

    fn value(&self) -> Option<Decimal18> {
        self.kernel.value()
    }

    fn samples(&self) -> &[IndicatorSample] {
        &self.samples
    }

    fn flush(&mut self) {
        self.close_open_bucket();
    }
}

/// A rolling fixed-capacity window with an O(1) running total.
#[derive(Clone, Debug, Default)]
pub struct RollingWindow {
    period: i64,
    values: VecDeque<Decimal18>,
    total: Decimal18,
}

impl RollingWindow {
    /// Create an empty window with a positive capacity.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] when `period` is not positive.
    pub fn new(period: i64) -> Result<Self, IndicatorError> {
        if period < 1 {
            return Err(IndicatorError::Configuration(
                "period must be a positive integer".to_string(),
            ));
        }
        Ok(Self {
            period,
            values: VecDeque::new(),
            total: Decimal18::ZERO,
        })
    }

    /// Push one value, evicting the oldest when over capacity.
    pub fn push(&mut self, value: Decimal18) {
        self.values.push_back(value);
        self.total = add(self.total, value);
        if self.values.len() as i64 > self.period {
            if let Some(oldest) = self.values.pop_front() {
                self.total = subtract(self.total, oldest);
            }
        }
    }

    /// Whether the window holds a full period.
    #[must_use]
    pub fn is_warm(&self) -> bool {
        self.values.len() as i64 >= self.period
    }

    /// The mean of the window, or `None` before warm-up.
    #[must_use]
    pub fn mean(&self) -> Option<Decimal18> {
        if !self.is_warm() {
            return None;
        }
        Some(divide(self.total, Decimal18::from_i64(self.period)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Accumulator {
        window: RollingWindow,
    }

    impl Accumulator {
        fn new(period: i64) -> Self {
            Self {
                window: RollingWindow::new(period).expect("window"),
            }
        }
    }

    impl IndicatorKernel for Accumulator {
        fn name(&self) -> &'static str {
            "accumulator"
        }

        fn drivers(&self) -> &'static [TickDriver] {
            &[TickDriver::Trade]
        }

        fn on_trade(&mut self, price: Decimal18, _quantity: Decimal18, _timestamp_ns: i64) {
            self.window.push(price);
        }

        fn value(&self) -> Option<Decimal18> {
            self.window.mean()
        }
    }

    fn trade(timestamp_ns: i64, sequence: u64, price: i64) -> Tick {
        Tick {
            timestamp_ns,
            sequence,
            kind: TickKind::Trade {
                price: Decimal18::from_i64(price),
                quantity: Decimal18::from_i64(1),
            },
        }
    }

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

    #[test]
    fn session_start_rolls_at_midnight() {
        // 2024-01-01T00:00:00Z exactly.
        let day = 19_723 * DAY_NS;
        assert_eq!(session_start_ns(day, MIDNIGHT_UTC_NS), day);
        assert_eq!(session_start_ns(day + DAY_NS - 1, MIDNIGHT_UTC_NS), day);
        assert_eq!(
            session_start_ns(day + DAY_NS, MIDNIGHT_UTC_NS),
            day + DAY_NS
        );
        // An anchor at noon keeps an 11:00 event in the previous session.
        let noon = DAY_NS / 2;
        assert_eq!(session_start_ns(day + noon - 1, noon), day - DAY_NS + noon);
        assert_eq!(session_start_ns(day + noon, noon), day + noon);
    }

    #[test]
    fn machine_emits_one_sample_per_non_empty_bucket() {
        let mut machine =
            IndicatorMachine::with_kernel(Accumulator::new(1), "spy", DEFAULT_BUCKET_NS, None)
                .expect("machine");
        machine.on_tick(&trade(0, 0, 10)).expect("tick");
        machine.on_tick(&trade(1, 1, 20)).expect("tick");
        // A gap bucket with no driver event emits nothing.
        machine
            .on_tick(&trade(2 * DEFAULT_BUCKET_NS, 2, 30))
            .expect("tick");
        machine.flush();
        let samples = machine.samples().to_vec();
        assert_eq!(samples.len(), 2);
        assert_eq!(samples[0].start_ns, 0);
        assert_eq!(samples[0].end_ns, DEFAULT_BUCKET_NS);
        assert_eq!(samples[1].start_ns, 2 * DEFAULT_BUCKET_NS);
        assert_eq!(samples[0].symbol, "SPY");
    }

    #[test]
    fn machine_flush_closes_the_trailing_bucket() {
        let mut machine =
            IndicatorMachine::with_kernel(Accumulator::new(1), "spy", DEFAULT_BUCKET_NS, None)
                .expect("machine");
        machine.on_tick(&trade(500, 0, 10)).expect("tick");
        machine.flush();
        assert_eq!(machine.samples().len(), 1);
        // Flush is idempotent.
        machine.flush();
        assert_eq!(machine.samples().len(), 1);
    }

    #[test]
    fn machine_resets_state_on_session_rollover() {
        #[derive(Default)]
        struct Counter {
            count: i64,
        }
        impl IndicatorKernel for Counter {
            fn name(&self) -> &'static str {
                "counter"
            }
            fn drivers(&self) -> &'static [TickDriver] {
                &[TickDriver::Trade]
            }
            fn on_trade(&mut self, _p: Decimal18, _q: Decimal18, _t: i64) {
                self.count += 1;
            }
            fn reset_session(&mut self) {
                self.count = 0;
            }
            fn value(&self) -> Option<Decimal18> {
                Some(Decimal18::from_i64(self.count))
            }
        }
        let mut machine = IndicatorMachine::with_kernel(
            Counter::default(),
            "spy",
            DEFAULT_BUCKET_NS,
            Some(MIDNIGHT_UTC_NS),
        )
        .expect("machine");
        let day = 19_723 * DAY_NS;
        machine.on_tick(&trade(day, 0, 10)).expect("tick");
        machine.on_tick(&trade(day + 1, 1, 10)).expect("tick");
        assert_eq!(machine.value(), Some(Decimal18::from_i64(2)));
        machine.on_tick(&trade(day + DAY_NS, 2, 10)).expect("tick");
        assert_eq!(machine.value(), Some(Decimal18::from_i64(1)));
    }
}
