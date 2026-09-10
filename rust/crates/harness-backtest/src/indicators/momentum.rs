//! Momentum indicators computed incrementally from tick events.

use harness_num::Decimal18;

use super::base::{
    add, divide, hundred, multiply, subtract, IndicatorError, OrderGuard, Tick, TickDriver,
    TickIndicator, TickKind,
};

/// Wilder relative strength index over consecutive trade-price changes.
#[derive(Clone, Debug)]
pub struct RelativeStrengthIndex {
    period: i64,
    previous_price: Option<Decimal18>,
    changes: i64,
    gain_total: Decimal18,
    loss_total: Decimal18,
    avg_gain: Option<Decimal18>,
    avg_loss: Option<Decimal18>,
    guard: OrderGuard,
}

impl RelativeStrengthIndex {
    /// Build an RSI with a positive period.
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
            previous_price: None,
            changes: 0,
            gain_total: Decimal18::ZERO,
            loss_total: Decimal18::ZERO,
            avg_gain: None,
            avg_loss: None,
            guard: OrderGuard::default(),
        })
    }
}

impl TickIndicator for RelativeStrengthIndex {
    fn name(&self) -> &'static str {
        "rsi"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_tick(&mut self, tick: &Tick) -> Result<(), IndicatorError> {
        self.guard.accept(tick.timestamp_ns, tick.sequence)?;
        let TickKind::Trade { price, .. } = &tick.kind else {
            return Ok(());
        };
        let price = *price;
        let previous = self.previous_price.replace(price);
        let Some(previous) = previous else {
            return Ok(());
        };
        let change = subtract(price, previous);
        let gain = if change.is_positive() {
            change
        } else {
            Decimal18::ZERO
        };
        let loss = if change.is_negative() {
            subtract(Decimal18::ZERO, change)
        } else {
            Decimal18::ZERO
        };
        if self.avg_gain.is_none() || self.avg_loss.is_none() {
            self.gain_total = add(self.gain_total, gain);
            self.loss_total = add(self.loss_total, loss);
            self.changes += 1;
            if self.changes == self.period {
                let divisor = Decimal18::from_i64(self.period);
                self.avg_gain = Some(divide(self.gain_total, divisor));
                self.avg_loss = Some(divide(self.loss_total, divisor));
            }
        } else {
            let divisor = Decimal18::from_i64(self.period);
            let avg_gain = self.avg_gain.expect("seeded");
            let avg_loss = self.avg_loss.expect("seeded");
            self.avg_gain = Some(add(avg_gain, divide(subtract(gain, avg_gain), divisor)));
            self.avg_loss = Some(add(avg_loss, divide(subtract(loss, avg_loss), divisor)));
        }
        Ok(())
    }

    fn value(&self) -> Option<Decimal18> {
        let avg_gain = self.avg_gain?;
        let avg_loss = self.avg_loss?;
        let total = add(avg_gain, avg_loss);
        if total.is_zero() {
            return Some(hundred());
        }
        Some(divide(multiply(hundred(), avg_gain), total))
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

    #[test]
    fn rsi_warms_up_after_period_plus_one_trades() {
        let mut rsi = RelativeStrengthIndex::new(3).expect("rsi");
        for (sequence, price) in [(0u64, 1i64), (1, 2), (2, 3)] {
            rsi.on_tick(&trade(sequence, price)).expect("tick");
        }
        assert!(rsi.value().is_none());
        rsi.on_tick(&trade(3, 4)).expect("tick");
        // All gains: average loss is zero, so RSI is 100.
        assert_eq!(rsi.value().expect("value"), hundred());
    }

    #[test]
    fn flat_stream_maps_to_one_hundred() {
        let mut rsi = RelativeStrengthIndex::new(2).expect("rsi");
        for sequence in 0..4 {
            rsi.on_tick(&trade(sequence, 5)).expect("tick");
        }
        assert_eq!(rsi.value().expect("value"), hundred());
    }

    #[test]
    fn ordering_is_enforced() {
        let mut rsi = RelativeStrengthIndex::new(2).expect("rsi");
        rsi.on_tick(&trade(5, 1)).expect("tick");
        assert!(rsi.on_tick(&trade(4, 1)).is_err());
    }
}
