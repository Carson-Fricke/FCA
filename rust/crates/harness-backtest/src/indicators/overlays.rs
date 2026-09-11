//! Price-overlay indicators computed incrementally from tick events.

use harness_num::Decimal18;

use super::base::{
    add, divide, multiply, subtract, IndicatorError, IndicatorKernel, IndicatorMachine,
    RollingWindow, TickDriver, DEFAULT_BUCKET_NS, MIDNIGHT_UTC_NS,
};

/// Rolling mean of the last `period` trade prices.
#[derive(Clone, Debug)]
pub struct SmaKernel {
    window: RollingWindow,
}

impl SmaKernel {
    /// Build a simple moving average kernel with a positive period.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive period.
    pub fn new(period: i64) -> Result<Self, IndicatorError> {
        Ok(Self {
            window: RollingWindow::new(period)?,
        })
    }
}

impl IndicatorKernel for SmaKernel {
    fn name(&self) -> &'static str {
        "sma"
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

/// Recursive EMA over trade prices, seeded on the first trade.
#[derive(Clone, Debug)]
pub struct EmaKernel {
    span: i64,
    alpha: Decimal18,
    count: i64,
    ema: Option<Decimal18>,
}

impl EmaKernel {
    /// Build an EMA kernel with a positive span.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive span.
    pub fn new(span: i64) -> Result<Self, IndicatorError> {
        if span < 1 {
            return Err(IndicatorError::Configuration(
                "span must be a positive integer".to_string(),
            ));
        }
        Ok(Self {
            span,
            alpha: divide(Decimal18::from_i64(2), Decimal18::from_i64(span + 1)),
            count: 0,
            ema: None,
        })
    }
}

impl IndicatorKernel for EmaKernel {
    fn name(&self) -> &'static str {
        "ema"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_trade(&mut self, price: Decimal18, _quantity: Decimal18, _timestamp_ns: i64) {
        self.count += 1;
        self.ema = Some(match self.ema {
            None => price,
            Some(previous) => add(previous, multiply(self.alpha, subtract(price, previous))),
        });
    }

    fn value(&self) -> Option<Decimal18> {
        if self.count < self.span {
            return None;
        }
        self.ema
    }
}

/// Rolling mean of the last `period` quote mid-prices.
#[derive(Clone, Debug)]
pub struct MidSmaKernel {
    window: RollingWindow,
}

impl MidSmaKernel {
    /// Build a mid-price SMA kernel with a positive period.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive period.
    pub fn new(period: i64) -> Result<Self, IndicatorError> {
        Ok(Self {
            window: RollingWindow::new(period)?,
        })
    }
}

impl IndicatorKernel for MidSmaKernel {
    fn name(&self) -> &'static str {
        "mid_sma"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Quote]
    }

    fn on_quote(&mut self, bid: Decimal18, ask: Decimal18, _timestamp_ns: i64) {
        let mid = divide(add(bid, ask), Decimal18::from_i64(2));
        self.window.push(mid);
    }

    fn value(&self) -> Option<Decimal18> {
        self.window.mean()
    }
}

/// Rolling mean of the last `period` trade prices.
pub type SimpleMovingAverage = IndicatorMachine<SmaKernel>;

impl IndicatorMachine<SmaKernel> {
    /// Build a symbol-less SMA for direct arithmetic use.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive period.
    pub fn new(period: i64) -> Result<Self, IndicatorError> {
        Self::with_kernel(
            SmaKernel::new(period)?,
            String::new(),
            DEFAULT_BUCKET_NS,
            Some(MIDNIGHT_UTC_NS),
        )
    }
}

/// Recursive EMA over trade prices, seeded on the first trade.
pub type ExponentialMovingAverage = IndicatorMachine<EmaKernel>;

impl IndicatorMachine<EmaKernel> {
    /// Build a symbol-less EMA for direct arithmetic use.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive span.
    pub fn new(span: i64) -> Result<Self, IndicatorError> {
        Self::with_kernel(
            EmaKernel::new(span)?,
            String::new(),
            DEFAULT_BUCKET_NS,
            Some(MIDNIGHT_UTC_NS),
        )
    }
}

/// Rolling mean of the last `period` quote mid-prices.
pub type MidSimpleMovingAverage = IndicatorMachine<MidSmaKernel>;

impl IndicatorMachine<MidSmaKernel> {
    /// Build a symbol-less mid-price SMA for direct arithmetic use.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a non-positive period.
    pub fn new(period: i64) -> Result<Self, IndicatorError> {
        Self::with_kernel(
            MidSmaKernel::new(period)?,
            String::new(),
            DEFAULT_BUCKET_NS,
            Some(MIDNIGHT_UTC_NS),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::base::{Tick, TickIndicator, TickKind};

    fn trade(sequence: u64, price: i64) -> Tick {
        Tick {
            timestamp_ns: sequence as i64,
            sequence,
            kind: TickKind::Trade {
                price: Decimal18::from_i64(price),
                quantity: Decimal18::from_i64(1),
            },
        }
    }

    fn quote(sequence: u64, bid: i64, ask: i64) -> Tick {
        Tick {
            timestamp_ns: sequence as i64,
            sequence,
            kind: TickKind::Quote {
                bid: Decimal18::from_i64(bid),
                ask: Decimal18::from_i64(ask),
            },
        }
    }

    #[test]
    fn sma_warms_up_and_rolls() {
        let mut sma = SimpleMovingAverage::new(2).expect("sma");
        sma.on_tick(&trade(0, 10)).expect("tick");
        assert!(sma.value().is_none());
        sma.on_tick(&trade(1, 20)).expect("tick");
        assert_eq!(sma.value().expect("value"), Decimal18::from_i64(15));
        sma.on_tick(&trade(2, 30)).expect("tick");
        assert_eq!(sma.value().expect("value"), Decimal18::from_i64(25));
    }

    #[test]
    fn ema_seeds_and_warms_up() {
        let mut ema = ExponentialMovingAverage::new(2).expect("ema");
        ema.on_tick(&trade(0, 10)).expect("tick");
        assert!(ema.value().is_none());
        ema.on_tick(&trade(1, 20)).expect("tick");
        // alpha = 2/3, ema = 10 + (20-10)*2/3 = 16.666...
        let value = ema.value().expect("value");
        assert!(value.to_string().starts_with("16.666"));
    }

    #[test]
    fn mid_sma_uses_quote_midpoints() {
        let mut sma = MidSimpleMovingAverage::new(2).expect("mid_sma");
        sma.on_tick(&quote(0, 100, 102)).expect("tick");
        sma.on_tick(&trade(1, 500)).expect("tick");
        assert!(sma.value().is_none(), "one quote is not enough");
        sma.on_tick(&quote(2, 200, 204)).expect("tick");
        // mids: 101 and 202 -> mean 151.5
        let value = sma.value().expect("value");
        assert_eq!(value.to_string(), "151.500000000000000000");
    }
}
