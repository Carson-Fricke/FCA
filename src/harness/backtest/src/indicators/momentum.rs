//! Momentum indicators computed incrementally from tick events.

use num::Decimal18;

use super::base::add;
use super::base::divide;
use super::base::hundred;
use super::base::multiply;
use super::base::subtract;
use super::base::IndicatorError;
use super::base::IndicatorKernel;
use super::base::IndicatorMachine;
use super::base::TickDriver;
use super::base::DEFAULT_BUCKET_NS;
use super::base::MIDNIGHT_UTC_NS;

/// Wilder relative strength index over consecutive trade-price changes.
#[derive(Clone, Debug)]
pub struct RsiKernel {
    period: i64,
    previous_price: Option<Decimal18>,
    changes: i64,
    gain_total: Decimal18,
    loss_total: Decimal18,
    avg_gain: Option<Decimal18>,
    avg_loss: Option<Decimal18>,
}

impl RsiKernel {
    /// Build an RSI kernel with a positive period.
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
        })
    }
}

impl IndicatorKernel for RsiKernel {
    fn name(&self) -> &'static str {
        "rsi"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_trade(&mut self, price: Decimal18, _quantity: Decimal18, _timestamp_ns: i64) {
        let previous = self.previous_price.replace(price);
        let Some(previous) = previous else {
            return;
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

/// Wilder relative strength index over consecutive trade-price changes.
pub type RelativeStrengthIndex = IndicatorMachine<RsiKernel>;

impl IndicatorMachine<RsiKernel> {
    /// Build a symbol-less RSI for direct arithmetic use.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] when `period` is not positive.
    pub fn new(period: i64) -> Result<Self, IndicatorError> {
        Self::with_kernel(
            RsiKernel::new(period)?,
            String::new(),
            DEFAULT_BUCKET_NS,
            Some(MIDNIGHT_UTC_NS),
        )
    }
}
