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

#[cfg(test)]
mod tests {
    use super::{TradeThrough, TradeThroughConfig};
    use crate::market::{OrderType, Side};
    use crate::simple_touch::testing::{
        dec, expect_decision, large_quote, make_order, make_trade, HUNDRED, THREE_HUNDRED,
    };
    use crate::simple_touch::{DecideOptions, Liquidity};

    const HALF: i128 = 500_000_000_000_000_000;

    #[test]
    fn strict_model_requires_a_print_beyond_the_limit() {
        let mut model = TradeThrough::new(TradeThroughConfig::default()).unwrap();
        let buy = make_order(
            "strict",
            Side::Buy,
            OrderType::Limit,
            THREE_HUNDRED,
            Some(HUNDRED + HALF),
            None,
        );

        assert!(model
            .decide(
                &buy,
                &large_quote("quote", HUNDRED, HUNDRED + HALF),
                &DecideOptions::default()
            )
            .unwrap()
            .is_none());
        assert!(model
            .decide(
                &buy,
                &make_trade("trade", HUNDRED + HALF, HUNDRED),
                &DecideOptions::default()
            )
            .unwrap()
            .is_none());

        let decision = model
            .decide(
                &buy,
                &make_trade("through", HUNDRED + HALF - 10_000_000_000_000_000, HUNDRED),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(
            decision,
            HUNDRED,
            HUNDRED + HALF - 10_000_000_000_000_000,
            Liquidity::Maker,
        );
    }

    #[test]
    fn print_qualification_is_directional() {
        let mut model = TradeThrough::new(TradeThroughConfig::default()).unwrap();
        let buy = make_order(
            "direction-buy",
            Side::Buy,
            OrderType::Limit,
            THREE_HUNDRED,
            Some(HUNDRED + HALF),
            None,
        );
        assert!(model
            .decide(
                &buy,
                &make_trade("at-limit", HUNDRED + HALF, HUNDRED),
                &DecideOptions::default()
            )
            .unwrap()
            .is_none());
        let buy_through = model
            .decide(
                &buy,
                &make_trade("through", HUNDRED + HALF - 10_000_000_000_000_000, HUNDRED),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(
            buy_through,
            HUNDRED,
            HUNDRED + HALF - 10_000_000_000_000_000,
            Liquidity::Maker,
        );

        let sell = make_order(
            "direction-sell",
            Side::Sell,
            OrderType::Limit,
            THREE_HUNDRED,
            Some(HUNDRED + HALF),
            None,
        );
        assert!(model
            .decide(
                &sell,
                &make_trade("at-limit", HUNDRED + HALF, HUNDRED),
                &DecideOptions::default()
            )
            .unwrap()
            .is_none());
        let sell_through = model
            .decide(
                &sell,
                &make_trade("through", HUNDRED + HALF + 10_000_000_000_000_000, HUNDRED),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(
            sell_through,
            HUNDRED,
            HUNDRED + HALF + 10_000_000_000_000_000,
            Liquidity::Maker,
        );
    }

    #[test]
    fn print_size_and_remainder_policy() {
        let mut cancel_model = TradeThrough::new(TradeThroughConfig {
            remainder_policy: crate::simple_touch::RemainderPolicy::Cancel,
            ..TradeThroughConfig::default()
        })
        .unwrap();
        let mut rest_model = TradeThrough::new(TradeThroughConfig::default()).unwrap();
        let cancel_order = make_order(
            "cancel",
            Side::Buy,
            OrderType::Limit,
            THREE_HUNDRED,
            Some(HUNDRED + HALF),
            None,
        );
        let rest_order = make_order(
            "rest",
            Side::Buy,
            OrderType::Limit,
            THREE_HUNDRED,
            Some(HUNDRED + HALF),
            None,
        );
        let print_event = make_trade("print", HUNDRED + HALF - 10_000_000_000_000_000, HUNDRED);

        let cancel_decision = cancel_model
            .decide(&cancel_order, &print_event, &DecideOptions::default())
            .unwrap();
        let rest_decision = rest_model
            .decide(&rest_order, &print_event, &DecideOptions::default())
            .unwrap();
        assert!(cancel_decision.is_some() && rest_decision.is_some());
        assert_eq!(
            cancel_decision.unwrap().quantity,
            rest_decision.unwrap().quantity
        );
        assert_eq!(rest_decision.unwrap().quantity, HUNDRED);
        assert!(cancel_model.is_cancelled(cancel_order.order_id));
        assert!(!rest_model.is_cancelled(rest_order.order_id));

        let second = rest_model
            .decide(
                &rest_order,
                &make_trade("trade-2", HUNDRED + HALF - 20_000_000_000_000_000, HUNDRED),
                &DecideOptions {
                    remaining_quantity: Some(dec(200)),
                    ..DecideOptions::default()
                },
            )
            .unwrap();
        assert!(second.is_some());
    }

    #[test]
    fn parameters_change_threshold_and_metadata() {
        let mut default_model = TradeThrough::new(TradeThroughConfig::default()).unwrap();
        let mut configured = TradeThrough::new(TradeThroughConfig {
            response_latency_us: 40,
            minimum_trade_through: 100_000_000_000_000_000,
            strict: false,
            remainder_policy: crate::simple_touch::RemainderPolicy::Rest,
        })
        .unwrap();
        let buy = make_order(
            "parameters",
            Side::Buy,
            OrderType::Limit,
            THREE_HUNDRED,
            Some(HUNDRED + HALF),
            None,
        );

        assert!(default_model
            .decide(
                &buy,
                &make_trade("trade", HUNDRED + HALF, HUNDRED),
                &DecideOptions::default()
            )
            .unwrap()
            .is_none());
        let decision = configured
            .decide(
                &buy,
                &make_trade("configured", HUNDRED + 400_000_000_000_000_000, HUNDRED),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(
            decision,
            HUNDRED,
            HUNDRED + 400_000_000_000_000_000,
            Liquidity::Maker,
        );
        assert_eq!(configured.metadata().response_latency_us, 40);
        assert_eq!(
            configured.metadata().minimum_trade_through,
            100_000_000_000_000_000
        );
    }

    #[test]
    fn rejects_an_invalid_minimum_trade_through() {
        assert!(TradeThrough::new(TradeThroughConfig {
            minimum_trade_through: -10_000_000_000_000_000,
            ..TradeThroughConfig::default()
        })
        .is_err());
    }
}
