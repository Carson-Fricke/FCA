//! Strict deterministic trade-print fill decisions, ported from
//! `src/harness/fills/trade_through.py`.
//!
//! Quote events never fill this model, including marketable limits. A buy limit
//! requires a print strictly below its limit by default; a sell limit requires
//! a print strictly above it. `minimum_trade_through` adds an exact price
//! distance. Setting `strict = false` makes a print at the limit qualifying
//! while retaining the print-only rule.

use crate::decimal;
use crate::market::{Error, MarketEvent, MarketOrder, OrderType, Side};
use crate::simple_touch::{
    is_triggered, resolve_remaining, validate_pair, DecideOptions, FillDecision, Liquidity,
    RemainderPolicy,
};

/// Independent configuration for the strict print-only model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TradeThroughConfig {
    /// Response latency metadata in integer microseconds.
    pub response_latency_us: i64,
    /// Exact required price distance beyond the limit.
    pub minimum_trade_through: i128,
    /// Whether a print at the limit qualifies.
    pub strict: bool,
    /// Venue-side treatment after a partial fill.
    pub remainder_policy: RemainderPolicy,
}

impl Default for TradeThroughConfig {
    fn default() -> Self {
        Self {
            response_latency_us: 0,
            minimum_trade_through: 0,
            strict: true,
            remainder_policy: RemainderPolicy::Rest,
        }
    }
}

/// Stable assumptions for a print-only trade-through plugin.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TradeThroughMetadata {
    /// Response latency in integer microseconds.
    pub response_latency_us: i64,
    /// Always true: displayed size constrains the fill.
    pub displayed_size_constrained: bool,
    /// Exact required price distance beyond the limit.
    pub minimum_trade_through: i128,
    /// Whether a print at the limit qualifies.
    pub strict: bool,
    /// Venue-side treatment after a partial fill.
    pub remainder_policy: RemainderPolicy,
}

/// Require qualifying trade prints for every executable order.
#[derive(Clone, Debug, Default)]
pub struct TradeThrough {
    config: TradeThroughConfig,
    cancelled: Vec<String>,
}

impl TradeThrough {
    /// Create a model, rejecting a negative minimum trade-through.
    pub fn new(config: TradeThroughConfig) -> Result<Self, Error> {
        if config.minimum_trade_through < 0 {
            return Err(Error::InvalidArgument);
        }
        Ok(Self {
            config,
            cancelled: Vec::new(),
        })
    }

    /// The model's execution metadata.
    pub fn metadata(&self) -> TradeThroughMetadata {
        TradeThroughMetadata {
            response_latency_us: self.config.response_latency_us,
            displayed_size_constrained: true,
            minimum_trade_through: self.config.minimum_trade_through,
            strict: self.config.strict,
            remainder_policy: self.config.remainder_policy,
        }
    }

    /// Return whether this model has canceled an order's remainder.
    pub fn is_cancelled(&self, order_id: &str) -> bool {
        self.cancelled.iter().any(|cancelled| cancelled == order_id)
    }

    /// Decide a fill for `order` against `snap`.
    pub fn decide<'a>(
        &mut self,
        order: &MarketOrder<'a>,
        snap: &MarketEvent<'a>,
        opts: &DecideOptions,
    ) -> Result<Option<FillDecision<'a>>, Error> {
        validate_pair(order, snap)?;
        if self.is_cancelled(order.order_id) {
            return Ok(None);
        }
        let remaining = resolve_remaining(order, opts)?;

        let trade = match snap {
            MarketEvent::Trade(trade) => trade,
            MarketEvent::Quote(_) => return Ok(None),
        };
        let mut available = trade.quantity;
        if let Some(explicit) = opts.available_quantity {
            if explicit <= 0 {
                return Ok(None);
            }
            available = explicit;
        }

        if matches!(order.order_type, OrderType::Stop | OrderType::StopLimit)
            && !opts.stop_triggered
            && !is_triggered(order, snap)?
        {
            return Ok(None);
        }
        if matches!(order.order_type, OrderType::Limit | OrderType::StopLimit)
            && !self.qualifies(order, trade.price)?
        {
            return Ok(None);
        }

        let quantity = remaining.min(available);
        if quantity <= 0 {
            return Ok(None);
        }
        let liquidity = if matches!(order.order_type, OrderType::Limit | OrderType::StopLimit) {
            Liquidity::Maker
        } else {
            Liquidity::Taker
        };
        let decision = FillDecision {
            order_id: order.order_id,
            quantity,
            price: trade.price,
            decision_price: trade.price,
            triggering_market: *snap,
            liquidity,
        };
        if quantity < remaining && self.config.remainder_policy == RemainderPolicy::Cancel {
            self.cancelled.push(order.order_id.to_owned());
        }
        Ok(Some(decision))
    }

    fn qualifies(&self, order: &MarketOrder<'_>, price: i128) -> Result<bool, Error> {
        let limit = order.limit_price.ok_or(Error::InvalidArgument)?;
        if order.side == Side::Buy {
            let threshold = decimal::sub(limit, self.config.minimum_trade_through)
                .map_err(|_| Error::OutOfRange)?;
            Ok(if self.config.strict {
                price < threshold
            } else {
                price <= threshold
            })
        } else {
            let threshold = decimal::add(limit, self.config.minimum_trade_through)
                .map_err(|_| Error::OutOfRange)?;
            Ok(if self.config.strict {
                price > threshold
            } else {
                price >= threshold
            })
        }
    }
}
