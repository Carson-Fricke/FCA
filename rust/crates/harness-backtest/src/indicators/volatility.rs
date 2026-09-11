//! Volatility indicators computed incrementally from tick events.
//!
//! Emission, ordering, session, and sample semantics come from
//! [`super::base::IndicatorMachine`].

use std::collections::VecDeque;

use harness_num::Decimal18;

use super::base::{
    add, divide, multiply, sqrt, subtract, IndicatorError, IndicatorKernel, IndicatorMachine,
    TickDriver, DEFAULT_BUCKET_NS, MIDNIGHT_UTC_NS,
};

/// Wilder ATR over per-bucket true ranges derived from trade ticks.
#[derive(Clone, Debug)]
pub struct AtrKernel {
    period: i64,
    high: Option<Decimal18>,
    low: Option<Decimal18>,
    close: Option<Decimal18>,
    previous_close: Option<Decimal18>,
    ranges: i64,
    range_total: Decimal18,
    avg_range: Option<Decimal18>,
}

impl AtrKernel {
    /// Build an ATR kernel with a positive period.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive period.
    pub fn new(period: i64) -> Result<Self, IndicatorError> {
        if period < 1 {
            return Err(IndicatorError::Configuration(
                "period must be a positive integer".to_string(),
            ));
        }
        Ok(Self {
            period,
            high: None,
            low: None,
            close: None,
            previous_close: None,
            ranges: 0,
            range_total: Decimal18::ZERO,
            avg_range: None,
        })
    }
}

impl IndicatorKernel for AtrKernel {
    fn name(&self) -> &'static str {
        "atr"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_trade(&mut self, price: Decimal18, _quantity: Decimal18, _timestamp_ns: i64) {
        match (self.high, self.low) {
            (Some(high), Some(low)) => {
                if price > high {
                    self.high = Some(price);
                }
                if price < low {
                    self.low = Some(price);
                }
            }
            _ => {
                self.high = Some(price);
                self.low = Some(price);
            }
        }
        self.close = Some(price);
    }

    fn on_bucket_open(&mut self, _start_ns: i64) {
        self.high = None;
        self.low = None;
        self.close = None;
    }

    fn on_bucket_close(&mut self, _end_ns: i64) {
        let (Some(high), Some(low), Some(close)) = (self.high, self.low, self.close) else {
            return;
        };
        let mut true_range = subtract(high, low);
        if let Some(previous_close) = self.previous_close {
            let high_gap = subtract(high, previous_close);
            let low_gap = subtract(low, previous_close);
            if high_gap > true_range {
                true_range = high_gap;
            }
            if subtract(Decimal18::ZERO, low_gap) > true_range {
                true_range = subtract(Decimal18::ZERO, low_gap);
            }
        }
        self.previous_close = Some(close);
        let divisor = Decimal18::from_i64(self.period);
        match self.avg_range {
            None => {
                self.range_total = add(self.range_total, true_range);
                self.ranges += 1;
                if self.ranges == self.period {
                    self.avg_range = Some(divide(self.range_total, divisor));
                }
            }
            Some(avg_range) => {
                self.avg_range = Some(add(
                    avg_range,
                    divide(subtract(true_range, avg_range), divisor),
                ));
            }
        }
    }

    fn value(&self) -> Option<Decimal18> {
        self.avg_range
    }
}

/// Rolling population standard deviation of tick-to-tick simple returns.
#[derive(Clone, Debug)]
pub struct RealizedVolKernel {
    window_ns: i64,
    entries: VecDeque<(i64, Decimal18)>,
    sum: Decimal18,
    sum_squares: Decimal18,
    count: i64,
    previous_price: Option<Decimal18>,
}

impl RealizedVolKernel {
    /// Build a realized-volatility kernel with a positive window.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive window.
    pub fn new(window_ns: i64) -> Result<Self, IndicatorError> {
        if window_ns < 1 {
            return Err(IndicatorError::Configuration(
                "window must be a positive duration".to_string(),
            ));
        }
        Ok(Self {
            window_ns,
            entries: VecDeque::new(),
            sum: Decimal18::ZERO,
            sum_squares: Decimal18::ZERO,
            count: 0,
            previous_price: None,
        })
    }
}

impl IndicatorKernel for RealizedVolKernel {
    fn name(&self) -> &'static str {
        "realized_vol"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_trade(&mut self, price: Decimal18, _quantity: Decimal18, timestamp_ns: i64) {
        let previous = self.previous_price.replace(price);
        let Some(previous) = previous else {
            return;
        };
        let entry = divide(subtract(price, previous), previous);
        self.entries.push_back((timestamp_ns, entry));
        self.sum = add(self.sum, entry);
        self.sum_squares = add(self.sum_squares, multiply(entry, entry));
        self.count += 1;
    }

    fn on_bucket_close(&mut self, end_ns: i64) {
        let cutoff = end_ns - self.window_ns;
        while let Some((timestamp_ns, _)) = self.entries.front() {
            if *timestamp_ns >= cutoff {
                break;
            }
            let (_, expired) = self.entries.pop_front().expect("front exists");
            self.sum = subtract(self.sum, expired);
            self.sum_squares = subtract(self.sum_squares, multiply(expired, expired));
            self.count -= 1;
        }
    }

    fn reset_session(&mut self) {
        self.entries.clear();
        self.sum = Decimal18::ZERO;
        self.sum_squares = Decimal18::ZERO;
        self.count = 0;
        self.previous_price = None;
    }

    fn value(&self) -> Option<Decimal18> {
        if self.count == 0 {
            return None;
        }
        let divisor = Decimal18::from_i64(self.count);
        let mean = divide(self.sum, divisor);
        let variance = subtract(divide(self.sum_squares, divisor), multiply(mean, mean));
        Some(sqrt(variance))
    }
}

/// Wilder average true range over per-bucket trade ranges.
pub type AverageTrueRange = IndicatorMachine<AtrKernel>;

impl IndicatorMachine<AtrKernel> {
    /// Build a symbol-less ATR for direct arithmetic use.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive period.
    pub fn new(period: i64) -> Result<Self, IndicatorError> {
        Self::with_kernel(
            AtrKernel::new(period)?,
            String::new(),
            DEFAULT_BUCKET_NS,
            Some(MIDNIGHT_UTC_NS),
        )
    }
}

/// Rolling standard deviation of tick-to-tick simple returns.
pub type RealizedVolatility = IndicatorMachine<RealizedVolKernel>;

impl IndicatorMachine<RealizedVolKernel> {
    /// Build a symbol-less realized-volatility indicator.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive window.
    pub fn new(window_ns: i64) -> Result<Self, IndicatorError> {
        Self::with_kernel(
            RealizedVolKernel::new(window_ns)?,
            String::new(),
            DEFAULT_BUCKET_NS,
            Some(MIDNIGHT_UTC_NS),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::super::base::{Tick, TickIndicator, TickKind, DAY_NS};
    use super::*;

    fn trade(timestamp_ns: i64, sequence: u64, price: &str) -> Tick {
        Tick {
            timestamp_ns,
            sequence,
            kind: TickKind::Trade {
                price: price.parse().expect("price"),
                quantity: Decimal18::from_i64(1),
            },
        }
    }

    #[test]
    fn atr_warms_up_over_trade_buckets() {
        let mut atr = AverageTrueRange::new(2).expect("atr");
        // Bucket 0: high 12, low 10, close 11 => range 2.
        atr.on_tick(&trade(0, 0, "10")).expect("tick");
        atr.on_tick(&trade(1, 1, "12")).expect("tick");
        atr.on_tick(&trade(2, 2, "11")).expect("tick");
        // Rotate into bucket 1: high 15, low 9, close 14.
        atr.on_tick(&trade(60_000_000_000, 3, "15")).expect("tick");
        atr.on_tick(&trade(60_000_000_001, 4, "9")).expect("tick");
        assert!(atr.value().is_none(), "one range is not enough");
        // Rotate into bucket 2: warm-up completes with the second range.
        atr.on_tick(&trade(120_000_000_000, 5, "20")).expect("tick");
        let value = atr.value().expect("value");
        // Range 1 = 2. Range 2 = max(20-15=5, |15-11|=4, |15-9|... ) = 6.
        // Seed average = (2 + 6) / 2 = 4.
        assert_eq!(value.to_string(), "4.000000000000000000");
    }

    #[test]
    fn realized_vol_is_zero_for_a_single_return_and_warms_on_two() {
        let mut vol = RealizedVolatility::new(60_000_000_000).expect("vol");
        vol.on_tick(&trade(0, 0, "100")).expect("tick");
        assert!(vol.value().is_none(), "no return yet");
        vol.on_tick(&trade(1, 1, "110")).expect("tick");
        // One return of 0.1 => population stddev 0.
        assert_eq!(vol.value(), Some(Decimal18::ZERO));
        vol.on_tick(&trade(2, 2, "99")).expect("tick");
        assert!(vol.value().expect("value").is_positive());
    }

    #[test]
    fn realized_vol_resets_on_session_rollover() {
        let mut vol = RealizedVolatility::new(DAY_NS).expect("vol");
        let day = 19_723 * DAY_NS;
        vol.on_tick(&trade(day, 0, "100")).expect("tick");
        vol.on_tick(&trade(day + 1, 1, "110")).expect("tick");
        assert!(vol.value().is_some());
        vol.on_tick(&trade(day + DAY_NS, 2, "120")).expect("tick");
        // The session reset clears the previous price, so one trade emits nothing.
        assert!(vol.value().is_none());
    }
}
