//! Volume-based indicators computed incrementally from tick events.
//!
//! The cumulative members (session VWAP, cumulative dollar volume, on-balance
//! volume) reset their per-session state on every session rollover. Emission,
//! ordering, and sample semantics come from
//! [`super::base::IndicatorMachine`].

use std::collections::VecDeque;

use num::Decimal18;

use super::base::{
    add, divide, multiply, subtract, IndicatorError, IndicatorKernel, IndicatorMachine, TickDriver,
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
