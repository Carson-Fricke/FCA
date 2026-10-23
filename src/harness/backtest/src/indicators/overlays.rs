//! Price-overlay indicators computed incrementally from tick events.

use num::Decimal18;

use super::base::add;
use super::base::divide;
use super::base::multiply;
use super::base::subtract;
use super::base::IndicatorError;
use super::base::IndicatorKernel;
use super::base::IndicatorMachine;
use super::base::RollingWindow;
use super::base::TickDriver;
use super::base::DEFAULT_BUCKET_NS;
use super::base::MIDNIGHT_UTC_NS;

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
