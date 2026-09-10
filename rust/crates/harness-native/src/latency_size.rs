//! Deterministic latency and displayed-size constrained fill decisions, ported
//! from `src/harness/fills/latency_size.py`.
//!
//! The model deliberately keeps response latency as plugin metadata (integer
//! microseconds): the simulated venue owns response scheduling, while this
//! plugin owns the execution assumptions that produced a `FillDecision`.
//! Marketable orders use the visible quote side; a non-marketable limit order
//! is pessimistically resting by default and needs a qualifying trade print.
//! Setting `require_trade_through = false` enables the explicitly optimistic
//! alternative of filling a limit inside the current spread at its limit price.

use crate::market::{Error, MarketEvent, MarketOrder, OrderType, Side};
use crate::simple_touch::{
    displayed_quantity, is_triggered, resolve_remaining, validate_pair, DecideOptions,
    FillDecision, Liquidity, RemainderPolicy,
};

/// Independent configuration for [`LatencySize`](LatencySize).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LatencySizeConfig {
    /// Response latency metadata in integer microseconds. Consumed by an
    /// execution adapter; it does not alter the decision snapshot.
    pub response_latency_us: i64,
    /// Venue-side treatment after a partial fill.
    pub remainder_policy: RemainderPolicy,
    /// Whether a resting limit needs a qualifying trade print.
    pub require_trade_through: bool,
}

impl Default for LatencySizeConfig {
    fn default() -> Self {
        Self {
            response_latency_us: 0,
            remainder_policy: RemainderPolicy::Rest,
            require_trade_through: true,
        }
    }
}

/// Stable execution metadata accompanying a latency-size plugin.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LatencyMetadata {
    /// Response latency in integer microseconds.
    pub response_latency_us: i64,
    /// Always true: displayed size constrains the fill.
    pub displayed_size_constrained: bool,
    /// Venue-side treatment after a partial fill.
    pub remainder_policy: RemainderPolicy,
    /// Whether a resting limit needs a qualifying trade print.
    pub require_trade_through: bool,
}

/// Fill single-leg orders with deterministic latency and size rules.
///
/// The model is stateful only for the cancel remainder policy: each order
/// identifier is independently marked when its first decision is partial. The
/// caller still supplies the authoritative remaining quantity on every
/// invocation.
#[derive(Clone, Debug, Default)]
pub struct LatencySize {
    config: LatencySizeConfig,
    cancelled: Vec<String>,
}

impl LatencySize {
    /// Create a model with the given configuration.
    pub fn new(config: LatencySizeConfig) -> Self {
        Self {
            config,
            cancelled: Vec::new(),
        }
    }

    /// The model's execution metadata.
    pub fn metadata(&self) -> LatencyMetadata {
        LatencyMetadata {
            response_latency_us: self.config.response_latency_us,
            displayed_size_constrained: true,
            remainder_policy: self.config.remainder_policy,
            require_trade_through: self.config.require_trade_through,
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
        let mut size = displayed_quantity(order.side, snap);
        if let Some(explicit) = opts.available_quantity {
            if explicit <= 0 {
                return Ok(None);
            }
            size = explicit;
        }

        if matches!(order.order_type, OrderType::Stop | OrderType::StopLimit)
            && !opts.stop_triggered
            && !is_triggered(order, snap)?
        {
            return Ok(None);
        }

        let Some((price, liquidity)) = self.execution(order, snap)? else {
            return Ok(None);
        };
        let quantity = remaining.min(size);
        if quantity <= 0 {
            return Ok(None);
        }
        let decision = FillDecision {
            order_id: order.order_id,
            quantity,
            price,
            decision_price: price,
            triggering_market: *snap,
            liquidity,
        };
        if quantity < remaining && self.config.remainder_policy == RemainderPolicy::Cancel {
            self.cancelled.push(order.order_id.to_owned());
        }
        Ok(Some(decision))
    }

    fn required_limit(order: &MarketOrder<'_>) -> Result<i128, Error> {
        order.limit_price.ok_or(Error::InvalidArgument)
    }

    fn qualifying_print(order: &MarketOrder<'_>, price: i128) -> Result<bool, Error> {
        let limit = Self::required_limit(order)?;
        Ok(if order.side == Side::Buy {
            price <= limit
        } else {
            price >= limit
        })
    }

    fn execution(
        &self,
        order: &MarketOrder<'_>,
        snap: &MarketEvent<'_>,
    ) -> Result<Option<(i128, Liquidity)>, Error> {
        let result = match snap {
            MarketEvent::Trade(trade) => {
                if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
                    Some((trade.price, Liquidity::Taker))
                } else if Self::qualifying_print(order, trade.price)? {
                    Some((trade.price, Liquidity::Maker))
                } else {
                    None
                }
            }
            MarketEvent::Quote(quote) => {
                if order.side == Side::Buy {
                    if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
                        Some((quote.ask_price, Liquidity::Taker))
                    } else {
                        let limit = Self::required_limit(order)?;
                        if quote.ask_price <= limit {
                            Some((quote.ask_price, Liquidity::Taker))
                        } else if !self.config.require_trade_through
                            && quote.bid_price < limit
                            && limit < quote.ask_price
                        {
                            Some((limit, Liquidity::Maker))
                        } else {
                            None
                        }
                    }
                } else if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
                    Some((quote.bid_price, Liquidity::Taker))
                } else {
                    let limit = Self::required_limit(order)?;
                    if quote.bid_price >= limit {
                        Some((quote.bid_price, Liquidity::Taker))
                    } else if !self.config.require_trade_through
                        && quote.bid_price < limit
                        && limit < quote.ask_price
                    {
                        Some((limit, Liquidity::Maker))
                    } else {
                        None
                    }
                }
            }
        };
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::{LatencySize, LatencySizeConfig, LatencyMetadata, RemainderPolicy};
    use crate::market::{OrderType, Side};
    use crate::simple_touch::testing::{
        dec, expect_decision, large_quote, make_order, make_trade, HUNDRED, ONE0ONE,
        THREE_HUNDRED,
    };
    use crate::simple_touch::{DecideOptions, Liquidity};

    #[test]
    fn displayed_size_bounds_fills_and_applies_remainder_policy() {
        let mut cancel_model = LatencySize::new(LatencySizeConfig {
            remainder_policy: RemainderPolicy::Cancel,
            ..LatencySizeConfig::default()
        });
        let mut rest_model = LatencySize::new(LatencySizeConfig::default());
        let buy = make_order(
            "size-cancel",
            Side::Buy,
            OrderType::Market,
            THREE_HUNDRED,
            None,
            None,
        );

        let first = cancel_model
            .decide(&buy, &large_quote("q-1", HUNDRED, ONE0ONE), &DecideOptions::default())
            .unwrap();
        expect_decision(first, HUNDRED, ONE0ONE, Liquidity::Taker);
        assert!(cancel_model.is_cancelled(buy.order_id));

        let cancelled_second = cancel_model
            .decide(
                &buy,
                &large_quote("q-2", HUNDRED, ONE0ONE),
                &DecideOptions {
                    remaining_quantity: Some(dec(200)),
                    ..DecideOptions::default()
                },
            )
            .unwrap();
        assert!(cancelled_second.is_none());

        let rest_first = rest_model
            .decide(&buy, &large_quote("q-1", HUNDRED, ONE0ONE), &DecideOptions::default())
            .unwrap();
        expect_decision(rest_first, HUNDRED, ONE0ONE, Liquidity::Taker);
        assert!(!rest_model.is_cancelled(buy.order_id));
        let rest_second = rest_model
            .decide(
                &buy,
                &large_quote("q-2", HUNDRED, ONE0ONE),
                &DecideOptions {
                    remaining_quantity: Some(dec(200)),
                    ..DecideOptions::default()
                },
            )
            .unwrap();
        expect_decision(rest_second, HUNDRED, ONE0ONE, Liquidity::Taker);
    }

    #[test]
    fn partial_fills_across_quote_events_are_snapshot_deterministic() {
        let mut model = LatencySize::new(LatencySizeConfig::default());
        let buy = make_order("snapshots", Side::Buy, OrderType::Market, THREE_HUNDRED, None, None);
        let first_quote = large_quote("q-1", HUNDRED, ONE0ONE);
        let second_quote = large_quote("q-2", HUNDRED, HUNDRED);

        let first = model.decide(&buy, &first_quote, &DecideOptions::default()).unwrap();
        let second = model
            .decide(
                &buy,
                &second_quote,
                &DecideOptions {
                    remaining_quantity: Some(dec(200)),
                    ..DecideOptions::default()
                },
            )
            .unwrap();
        expect_decision(first, HUNDRED, ONE0ONE, Liquidity::Taker);
        expect_decision(second, HUNDRED, HUNDRED, Liquidity::Taker);
        assert_eq!(first.unwrap().triggering_market.event_id(), "q-1");
        assert_eq!(second.unwrap().triggering_market.event_id(), "q-2");

        let repeat = model
            .decide(
                &buy,
                &first_quote,
                &DecideOptions {
                    remaining_quantity: Some(HUNDRED),
                    ..DecideOptions::default()
                },
            )
            .unwrap();
        assert!(repeat.is_some());
        assert_eq!(first.unwrap().quantity, repeat.unwrap().quantity);
        assert_eq!(first.unwrap().price, repeat.unwrap().price);
    }

    #[test]
    fn inside_spread_limit_requires_a_qualifying_print_by_default() {
        let mut model = LatencySize::new(LatencySizeConfig::default());
        let buy = make_order(
            "inside",
            Side::Buy,
            OrderType::Limit,
            THREE_HUNDRED,
            Some(HUNDRED + 500_000_000_000_000_000),
            None,
        );

        assert!(model
            .decide(&buy, &large_quote("q-1", HUNDRED, ONE0ONE), &DecideOptions::default())
            .unwrap()
            .is_none());
        assert!(model
            .decide(
                &buy,
                &make_trade("t-1", HUNDRED + 750_000_000_000_000_000, HUNDRED),
                &DecideOptions::default()
            )
            .unwrap()
            .is_none());

        let decision = model
            .decide(
                &buy,
                &make_trade("t-2", HUNDRED + 500_000_000_000_000_000, HUNDRED),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(
            decision,
            HUNDRED,
            HUNDRED + 500_000_000_000_000_000,
            Liquidity::Maker,
        );
    }

    #[test]
    fn configuration_changes_latency_and_inside_spread_outcome() {
        let mut pessimistic = LatencySize::new(LatencySizeConfig {
            response_latency_us: 25,
            require_trade_through: true,
            ..LatencySizeConfig::default()
        });
        let mut optimistic = LatencySize::new(LatencySizeConfig {
            response_latency_us: 50,
            require_trade_through: false,
            ..LatencySizeConfig::default()
        });
        let buy = make_order(
            "parameters",
            Side::Buy,
            OrderType::Limit,
            THREE_HUNDRED,
            Some(HUNDRED + 500_000_000_000_000_000),
            None,
        );
        let inside = large_quote("q-1", HUNDRED, ONE0ONE);

        assert_eq!(pessimistic.metadata().response_latency_us, 25);
        assert!(pessimistic
            .decide(&buy, &inside, &DecideOptions::default())
            .unwrap()
            .is_none());
        let decision = optimistic.decide(&buy, &inside, &DecideOptions::default()).unwrap();
        expect_decision(
            decision,
            HUNDRED,
            HUNDRED + 500_000_000_000_000_000,
            Liquidity::Maker,
        );
        assert_eq!(optimistic.metadata().response_latency_us, 50);
        assert_eq!(
            pessimistic.metadata(),
            LatencyMetadata {
                response_latency_us: 25,
                displayed_size_constrained: true,
                remainder_policy: RemainderPolicy::Rest,
                require_trade_through: true,
            }
        );
    }
}
