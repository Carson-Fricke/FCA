//! Deterministic top-of-book touch decisions for single-leg orders.

use harness_num::Decimal18;

use crate::error::{LiveError, LiveResult};
use crate::events::MarketEvent;
use crate::fills::decision::FillDecision;
use crate::orders::{OrderType, Side, SingleLegOrder};

/// Fill a single-leg order at the first visible quote or trade touch.
///
/// Quotes use the ask for buys and bid for sells. A quote's displayed side
/// quantity bounds a fill, giving the simulated venue deterministic
/// partial-fill behavior without a random participation model.
#[derive(Clone, Copy, Debug, Default)]
pub struct SimpleTouch;

impl SimpleTouch {
    /// Construct the stateless model.
    pub const fn new() -> Self {
        Self
    }

    /// Return a fill decision, or `None` when the order does not touch.
    pub fn decide(
        &self,
        order: &SingleLegOrder,
        market: &MarketEvent,
        remaining_quantity: Option<Decimal18>,
        available_quantity: Option<Decimal18>,
        stop_triggered: bool,
    ) -> LiveResult<Option<FillDecision>> {
        validate(order, market)?;
        let remaining = match remaining_quantity {
            Some(value) => value,
            None => order.quantity,
        };
        if !remaining.is_positive() {
            return Err(LiveError::Fill(
                "remaining_quantity must be positive".into(),
            ));
        }
        let available = match available_quantity {
            Some(value) if value.is_positive() => value,
            Some(_) => return Ok(None),
            None => market_quantity(order.side, market),
        };

        if matches!(order.order_type, OrderType::Stop | OrderType::StopLimit)
            && !stop_triggered
            && !self.is_triggered(order, market)?
        {
            return Ok(None);
        }

        let Some((price, touched)) = touch(order, market) else {
            return Ok(None);
        };
        if !touched {
            return Ok(None);
        }
        let quantity = if remaining < available {
            remaining
        } else {
            available
        };
        if !quantity.is_positive() {
            return Ok(None);
        }
        Ok(Some(FillDecision::new(
            &order.order_id,
            quantity,
            price,
            price,
            market.clone(),
            "taker",
        )?))
    }

    /// Return whether a stop order is armed by this market snapshot.
    pub fn is_triggered(
        &self,
        order: &SingleLegOrder,
        market: &MarketEvent,
    ) -> LiveResult<bool> {
        validate(order, market)?;
        if !matches!(order.order_type, OrderType::Stop | OrderType::StopLimit) {
            return Ok(true);
        }
        let Some(stop_price) = order.stop_price else {
            return Err(LiveError::Fill("stop order is missing stop_price".into()));
        };
        Ok(match market {
            MarketEvent::Trade(trade) => {
                if order.side == Side::Buy {
                    trade.price >= stop_price
                } else {
                    trade.price <= stop_price
                }
            }
            MarketEvent::Quote(quote) => {
                if order.side == Side::Buy {
                    quote.ask_price >= stop_price
                } else {
                    quote.bid_price <= stop_price
                }
            }
        })
    }
}

pub(crate) fn validate(order: &SingleLegOrder, market: &MarketEvent) -> LiveResult<()> {
    if order.symbol != *market.symbol() {
        return Err(LiveError::Fill(
            "order and market symbols must match".into(),
        ));
    }
    Ok(())
}

pub(crate) fn market_quantity(side: Side, market: &MarketEvent) -> Decimal18 {
    match market {
        MarketEvent::Trade(trade) => trade.quantity,
        MarketEvent::Quote(quote) => {
            if side == Side::Buy {
                quote.ask_quantity
            } else {
                quote.bid_quantity
            }
        }
    }
}

fn touch(order: &SingleLegOrder, market: &MarketEvent) -> Option<(Decimal18, bool)> {
    match market {
        MarketEvent::Trade(trade) => {
            let price = trade.price;
            if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
                Some((price, true))
            } else {
                None
            }
        }
        MarketEvent::Quote(quote) => {
            if order.side == Side::Buy {
                let price = quote.ask_price;
                if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
                    return Some((price, true));
                }
                let limit = order.limit_price?;
                Some((price, price <= limit))
            } else {
                let price = quote.bid_price;
                if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
                    return Some((price, true));
                }
                let limit = order.limit_price?;
                Some((price, price >= limit))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use harness_num::Decimal18;

    use super::SimpleTouch;
    use crate::events::{MarketEvent, QuoteEvent};
    use crate::orders::{OrderType, Side, SingleLegOrder, TimeInForce};
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    fn order(order_type: OrderType, limit: Option<&str>) -> SingleLegOrder {
        SingleLegOrder::new(
            "o1",
            Symbol::new("AAPL").unwrap(),
            Side::Buy,
            dec("10"),
            order_type,
            TimeInForce::Day,
            limit.map(dec),
            None,
            None,
        )
        .unwrap()
    }

    fn quote(ask: &str, ask_qty: &str) -> MarketEvent {
        MarketEvent::Quote(
            QuoteEvent::new(
                Symbol::new("AAPL").unwrap(),
                dec("100"),
                dec("5"),
                dec(ask),
                dec(ask_qty),
                None,
            )
            .unwrap(),
        )
    }

    #[test]
    fn market_order_fills_at_ask_bounded_by_size() {
        let decision = SimpleTouch::new()
            .decide(&order(OrderType::Market, None), &quote("101", "3"), None, None, false)
            .unwrap()
            .unwrap();
        assert_eq!(decision.quantity, dec("3"));
        assert_eq!(decision.price, dec("101"));
        assert_eq!(decision.liquidity, "taker");
    }

    #[test]
    fn resting_limit_does_not_fill_above_limit() {
        let decision = SimpleTouch::new()
            .decide(
                &order(OrderType::Limit, Some("100")),
                &quote("101", "3"),
                None,
                None,
                false,
            )
            .unwrap();
        assert!(decision.is_none());
    }

    #[test]
    fn limit_fills_when_touch_crosses() {
        let decision = SimpleTouch::new()
            .decide(
                &order(OrderType::Limit, Some("101")),
                &quote("101", "3"),
                None,
                None,
                false,
            )
            .unwrap()
            .unwrap();
        assert_eq!(decision.price, dec("101"));
    }
}
