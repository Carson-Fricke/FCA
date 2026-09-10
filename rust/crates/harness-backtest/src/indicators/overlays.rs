//! Price-overlay indicators computed incrementally from tick events.

use std::collections::VecDeque;

use harness_num::Decimal18;

use super::base::{
    add, divide, multiply, subtract, IndicatorError, OrderGuard, Tick, TickDriver, TickIndicator,
    TickKind,
};

/// Rolling mean of the last `period` trade prices.
#[derive(Clone, Debug)]
pub struct SimpleMovingAverage {
    period: i64,
    window: VecDeque<Decimal18>,
    total: Decimal18,
    guard: OrderGuard,
}

impl SimpleMovingAverage {
    /// Build an SMA with a positive period.
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
            window: VecDeque::new(),
            total: Decimal18::ZERO,
            guard: OrderGuard::default(),
        })
    }

    fn push(&mut self, value: Decimal18) {
        self.window.push_back(value);
        self.total = add(self.total, value);
        if self.window.len() as i64 > self.period {
            if let Some(oldest) = self.window.pop_front() {
                self.total = subtract(self.total, oldest);
            }
        }
    }
}

impl TickIndicator for SimpleMovingAverage {
    fn name(&self) -> &'static str {
        "sma"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_tick(&mut self, tick: &Tick) -> Result<(), IndicatorError> {
        self.guard.accept(tick.timestamp_ns, tick.sequence)?;
        if let TickKind::Trade { price, .. } = &tick.kind {
            self.push(*price);
        }
        Ok(())
    }

    fn value(&self) -> Option<Decimal18> {
        if (self.window.len() as i64) < self.period {
            return None;
        }
        Some(divide(self.total, Decimal18::from_i64(self.period)))
    }
}

/// Recursive EMA over trade prices, seeded on the first trade.
#[derive(Clone, Debug)]
pub struct ExponentialMovingAverage {
    span: i64,
    alpha: Decimal18,
    count: i64,
    ema: Option<Decimal18>,
    guard: OrderGuard,
}

impl ExponentialMovingAverage {
    /// Build an EMA with a positive span.
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
            guard: OrderGuard::default(),
        })
    }
}

impl TickIndicator for ExponentialMovingAverage {
    fn name(&self) -> &'static str {
        "ema"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_tick(&mut self, tick: &Tick) -> Result<(), IndicatorError> {
        self.guard.accept(tick.timestamp_ns, tick.sequence)?;
        if let TickKind::Trade { price, .. } = &tick.kind {
            self.count += 1;
            self.ema = Some(match self.ema {
                None => *price,
                Some(previous) => add(previous, multiply(self.alpha, subtract(*price, previous))),
            });
        }
        Ok(())
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
pub struct MidSimpleMovingAverage {
    period: i64,
    window: VecDeque<Decimal18>,
    total: Decimal18,
    guard: OrderGuard,
}

impl MidSimpleMovingAverage {
    /// Build a mid-price SMA with a positive period.
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
            window: VecDeque::new(),
            total: Decimal18::ZERO,
            guard: OrderGuard::default(),
        })
    }
}

impl TickIndicator for MidSimpleMovingAverage {
    fn name(&self) -> &'static str {
        "mid_sma"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Quote]
    }

    fn on_tick(&mut self, tick: &Tick) -> Result<(), IndicatorError> {
        self.guard.accept(tick.timestamp_ns, tick.sequence)?;
        if let TickKind::Quote { bid, ask } = &tick.kind {
            let mid = divide(add(*bid, *ask), Decimal18::from_i64(2));
            self.window.push_back(mid);
            self.total = add(self.total, mid);
            if self.window.len() as i64 > self.period {
                if let Some(oldest) = self.window.pop_front() {
                    self.total = subtract(self.total, oldest);
                }
            }
        }
        Ok(())
    }

    fn value(&self) -> Option<Decimal18> {
        if (self.window.len() as i64) < self.period {
            return None;
        }
        Some(divide(self.total, Decimal18::from_i64(self.period)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
