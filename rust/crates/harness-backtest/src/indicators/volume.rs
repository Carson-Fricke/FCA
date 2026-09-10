//! Volume-based indicators computed incrementally from tick events.
//!
//! The cumulative members (session VWAP, cumulative dollar volume, on-balance
//! volume) reset their per-session state on every session rollover. Emission,
//! ordering, and sample semantics come from
//! [`super::base::IndicatorMachine`].

use std::collections::VecDeque;

use harness_num::Decimal18;

use super::base::{
    add, divide, multiply, subtract, IndicatorError, IndicatorKernel, IndicatorMachine,
    TickDriver,
};

/// Session-cumulative volume-weighted average price of trades.
#[derive(Clone, Debug, Default)]
pub struct VwapKernel {
    notional: Decimal18,
    volume: Decimal18,
}

impl VwapKernel {
    /// Build an empty VWAP kernel.
    #[must_use]
    pub fn new() -> Self {
        Self {
            notional: Decimal18::ZERO,
            volume: Decimal18::ZERO,
        }
    }
}

impl IndicatorKernel for VwapKernel {
    fn name(&self) -> &'static str {
        "vwap"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_trade(&mut self, price: Decimal18, quantity: Decimal18, _timestamp_ns: i64) {
        self.notional = add(self.notional, multiply(price, quantity));
        self.volume = add(self.volume, quantity);
    }

    fn reset_session(&mut self) {
        self.notional = Decimal18::ZERO;
        self.volume = Decimal18::ZERO;
    }

    fn value(&self) -> Option<Decimal18> {
        if self.volume.is_zero() {
            return None;
        }
        Some(divide(self.notional, self.volume))
    }
}

/// Traded notional, cumulative per session or rolling over a duration.
#[derive(Clone, Debug)]
pub struct DollarVolumeKernel {
    window_ns: Option<i64>,
    entries: VecDeque<(i64, Decimal18)>,
    notional: Decimal18,
}

impl DollarVolumeKernel {
    /// Build a dollar-volume kernel; `window_ns` selects rolling mode.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive window.
    pub fn new(window_ns: Option<i64>) -> Result<Self, IndicatorError> {
        if let Some(window) = window_ns {
            if window < 1 {
                return Err(IndicatorError::Configuration(
                    "window must be a positive duration".to_string(),
                ));
            }
        }
        Ok(Self {
            window_ns,
            entries: VecDeque::new(),
            notional: Decimal18::ZERO,
        })
    }
}

impl IndicatorKernel for DollarVolumeKernel {
    fn name(&self) -> &'static str {
        "dollar_volume"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_trade(&mut self, price: Decimal18, quantity: Decimal18, timestamp_ns: i64) {
        let notional = multiply(price, quantity);
        if self.window_ns.is_some() {
            self.entries.push_back((timestamp_ns, notional));
        }
        self.notional = add(self.notional, notional);
    }

    fn on_bucket_close(&mut self, end_ns: i64) {
        let Some(window) = self.window_ns else {
            return;
        };
        let cutoff = end_ns - window;
        while let Some((timestamp_ns, _)) = self.entries.front() {
            if *timestamp_ns >= cutoff {
                break;
            }
            let (_, expired) = self.entries.pop_front().expect("front exists");
            self.notional = subtract(self.notional, expired);
        }
    }

    fn reset_session(&mut self) {
        self.entries.clear();
        self.notional = Decimal18::ZERO;
    }

    fn value(&self) -> Option<Decimal18> {
        Some(self.notional)
    }
}

/// On-balance volume from the tick rule, cumulative per session.
#[derive(Clone, Debug, Default)]
pub struct ObvKernel {
    obv: Decimal18,
    previous_price: Option<Decimal18>,
    last_sign: i32,
}

impl ObvKernel {
    /// Build an empty on-balance-volume kernel.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }
}

impl IndicatorKernel for ObvKernel {
    fn name(&self) -> &'static str {
        "obv"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_trade(&mut self, price: Decimal18, quantity: Decimal18, _timestamp_ns: i64) {
        let previous = self.previous_price.replace(price);
        let sign = match previous {
            None => 0,
            Some(previous) if price > previous => 1,
            Some(previous) if price < previous => -1,
            Some(_) => self.last_sign,
        };
        self.last_sign = sign;
        if sign > 0 {
            self.obv = add(self.obv, quantity);
        } else if sign < 0 {
            self.obv = subtract(self.obv, quantity);
        }
    }

    fn reset_session(&mut self) {
        self.obv = Decimal18::ZERO;
        self.previous_price = None;
        self.last_sign = 0;
    }

    fn value(&self) -> Option<Decimal18> {
        Some(self.obv)
    }
}

/// Session-cumulative volume-weighted average price of trades.
pub type VolumeWeightedAveragePrice = IndicatorMachine<VwapKernel>;

/// Traded notional, cumulative per session or rolling over a duration.
pub type DollarVolume = IndicatorMachine<DollarVolumeKernel>;

/// On-balance volume from the tick rule, cumulative per session.
pub type OnBalanceVolume = IndicatorMachine<ObvKernel>;

#[cfg(test)]
mod tests {
    use super::super::base::{
        Tick, TickIndicator, TickKind, DAY_NS, DEFAULT_BUCKET_NS, MIDNIGHT_UTC_NS,
    };
    use super::*;

    fn trade(timestamp_ns: i64, sequence: u64, price: i64, quantity: i64) -> Tick {
        Tick {
            timestamp_ns,
            sequence,
            kind: TickKind::Trade {
                price: Decimal18::from_i64(price),
                quantity: Decimal18::from_i64(quantity),
            },
        }
    }

    fn vwap() -> VolumeWeightedAveragePrice {
        IndicatorMachine::with_kernel(
            VwapKernel::new(),
            "SPY",
            DEFAULT_BUCKET_NS,
            Some(MIDNIGHT_UTC_NS),
        )
        .expect("vwap")
    }

    #[test]
    fn vwap_is_notional_over_volume() {
        let mut vwap = vwap();
        vwap.on_tick(&trade(0, 0, 10, 2)).expect("tick");
        vwap.on_tick(&trade(1, 1, 20, 1)).expect("tick");
        // (10*2 + 20*1) / 3 = 40/3 = 13.333...
        assert!(vwap.value().expect("value").to_string().starts_with("13.333"));
    }

    #[test]
    fn vwap_resets_each_session() {
        let mut vwap = vwap();
        let day = 19_723 * DAY_NS;
        vwap.on_tick(&trade(day, 0, 10, 1)).expect("tick");
        assert!(vwap.value().is_some());
        vwap.on_tick(&trade(day + DAY_NS, 1, 10, 1)).expect("tick");
        assert!(vwap.value().is_some());
        // A fresh session yields the first trade's price, not a blended one.
        assert_eq!(
            vwap.value().expect("value"),
            Decimal18::from_i64(10)
        );
    }

    #[test]
    fn dollar_volume_cumulative_and_rolling() {
        let mut cumulative =
            IndicatorMachine::with_kernel(DollarVolumeKernel::new(None).expect("dv"), "SPY", DEFAULT_BUCKET_NS, None)
                .expect("dv");
        cumulative.on_tick(&trade(0, 0, 10, 2)).expect("tick");
        cumulative.on_tick(&trade(1, 1, 20, 1)).expect("tick");
        assert_eq!(
            cumulative.value().expect("value"),
            Decimal18::from_i64(40)
        );

        let mut rolling = IndicatorMachine::with_kernel(
            DollarVolumeKernel::new(Some(60_000_000_000)).expect("dv"),
            "SPY",
            DEFAULT_BUCKET_NS,
            None,
        )
        .expect("dv");
        rolling.on_tick(&trade(0, 0, 10, 1)).expect("tick");
        // Rotating two buckets later and flushing expires the first trade.
        rolling.on_tick(&trade(120_000_000_000, 1, 10, 1)).expect("tick");
        rolling.flush();
        assert_eq!(rolling.value().expect("value"), Decimal18::from_i64(10));
    }

    #[test]
    fn obv_signs_against_the_previous_trade() {
        let mut obv = IndicatorMachine::with_kernel(
            ObvKernel::new(),
            "SPY",
            DEFAULT_BUCKET_NS,
            None,
        )
        .expect("obv");
        obv.on_tick(&trade(0, 0, 10, 5)).expect("tick");
        assert_eq!(obv.value(), Some(Decimal18::ZERO));
        obv.on_tick(&trade(1, 1, 11, 2)).expect("tick");
        assert_eq!(obv.value(), Some(Decimal18::from_i64(2)));
        // A flat trade repeats the previous sign.
        obv.on_tick(&trade(2, 2, 11, 3)).expect("tick");
        assert_eq!(obv.value(), Some(Decimal18::from_i64(5)));
        obv.on_tick(&trade(3, 3, 9, 4)).expect("tick");
        assert_eq!(obv.value(), Some(Decimal18::from_i64(1)));
    }
}
