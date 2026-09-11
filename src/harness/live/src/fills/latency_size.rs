//! Deterministic latency and displayed-size constrained fill decisions.

use std::collections::HashSet;

use num::Decimal18;

use crate::error::{LiveError, LiveResult};
use crate::events::MarketEvent;
use crate::fills::decision::FillDecision;
use crate::fills::simple_touch::{validate, SimpleTouch};
use crate::orders::{OrderType, Side, SingleLegOrder};

/// Venue-side treatment represented by a plugin after a partial fill.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RemainderPolicy {
    /// Cancel the remaining quantity.
    Cancel,
    /// Leave the remainder resting.
    Rest,
}

/// Fill single-leg orders with deterministic latency and size rules.
///
/// Marketable orders use the visible quote side. A non-marketable limit order
/// is pessimistically resting by default and needs a qualifying trade print.
/// Setting `require_trade_through=false` enables the explicitly optimistic
/// alternative of filling a limit inside the current spread at its limit.
#[derive(Clone, Debug)]
pub struct LatencySize {
    /// Response latency metadata, in microseconds.
    pub response_latency_us: u64,
    /// Venue-side remainder treatment after a partial fill.
    pub remainder_policy: RemainderPolicy,
    /// Require a qualifying trade print for resting limits when `true`.
    pub require_trade_through: bool,
    cancelled: HashSet<String>,
}

impl Default for LatencySize {
    fn default() -> Self {
        Self {
            response_latency_us: 0,
            remainder_policy: RemainderPolicy::Rest,
            require_trade_through: true,
            cancelled: HashSet::new(),
        }
    }
}

impl LatencySize {
    /// Construct a validated latency-size model.
    pub fn new(
        response_latency_us: u64,
        remainder_policy: RemainderPolicy,
        require_trade_through: bool,
    ) -> Self {
        Self {
            response_latency_us,
            remainder_policy,
            require_trade_through,
            cancelled: HashSet::new(),
        }
    }

    /// Order identifiers whose remainder this model canceled.
    pub fn cancelled_order_ids(&self) -> &HashSet<String> {
        &self.cancelled
    }

    /// Whether this model has canceled an order's remainder.
    pub fn is_cancelled(&self, order_id: &str) -> bool {
        self.cancelled.contains(order_id)
    }

    /// Return one size-bounded decision, or `None` when not executable.
    pub fn decide(
        &mut self,
        order: &SingleLegOrder,
        market: &MarketEvent,
        remaining_quantity: Option<Decimal18>,
        available_quantity: Option<Decimal18>,
        stop_triggered: bool,
    ) -> LiveResult<Option<FillDecision>> {
        validate(order, market)?;
        if self.cancelled.contains(&order.order_id) {
            return Ok(None);
        }
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
            None => displayed_quantity(order.side, market),
        };

        if matches!(order.order_type, OrderType::Stop | OrderType::StopLimit)
            && !stop_triggered
            && !SimpleTouch::new().is_triggered(order, market)?
        {
            return Ok(None);
        }

        let Some((price, liquidity)) = self.execution(order, market)? else {
            return Ok(None);
        };
        let quantity = if remaining < available {
            remaining
        } else {
            available
        };
        if !quantity.is_positive() {
            return Ok(None);
        }
        let decision = FillDecision::new(
            &order.order_id,
            quantity,
            price,
            price,
            market.clone(),
            liquidity,
        )?;
        if quantity < remaining && self.remainder_policy == RemainderPolicy::Cancel {
            self.cancelled.insert(order.order_id.clone());
        }
        Ok(Some(decision))
    }

    fn execution(
        &self,
        order: &SingleLegOrder,
        market: &MarketEvent,
    ) -> LiveResult<Option<(Decimal18, &'static str)>> {
        match market {
            MarketEvent::Trade(trade) => {
                if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
                    return Ok(Some((trade.price, "taker")));
                }
                if self.qualifying_print(order, trade.price)? {
                    return Ok(Some((trade.price, "maker")));
                }
                Ok(None)
            }
            MarketEvent::Quote(quote) => {
                if order.side == Side::Buy {
                    let price = quote.ask_price;
                    if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
                        return Ok(Some((price, "taker")));
                    }
                    let limit = required_limit(order)?;
                    if price <= limit {
                        return Ok(Some((price, "taker")));
                    }
                    if !self.require_trade_through
                        && quote.bid_price < limit
                        && limit < quote.ask_price
                    {
                        return Ok(Some((limit, "maker")));
                    }
                    Ok(None)
                } else {
                    let price = quote.bid_price;
                    if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
                        return Ok(Some((price, "taker")));
                    }
                    let limit = required_limit(order)?;
                    if price >= limit {
                        return Ok(Some((price, "taker")));
                    }
                    if !self.require_trade_through
                        && quote.bid_price < limit
                        && limit < quote.ask_price
                    {
                        return Ok(Some((limit, "maker")));
                    }
                    Ok(None)
                }
            }
        }
    }

    fn qualifying_print(&self, order: &SingleLegOrder, price: Decimal18) -> LiveResult<bool> {
        let limit = required_limit(order)?;
        Ok(if order.side == Side::Buy {
            price <= limit
        } else {
            price >= limit
        })
    }
}

fn required_limit(order: &SingleLegOrder) -> LiveResult<Decimal18> {
    order
        .limit_price
        .ok_or_else(|| LiveError::Fill("limit order is missing limit_price".into()))
}

fn displayed_quantity(side: Side, market: &MarketEvent) -> Decimal18 {
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

#[cfg(test)]
mod tests {
    use num::Decimal18;

    use super::{LatencySize, RemainderPolicy};
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

    fn quote(bid: &str, ask: &str) -> MarketEvent {
        MarketEvent::Quote(
            QuoteEvent::new(
                Symbol::new("AAPL").unwrap(),
                dec(bid),
                dec("5"),
                dec(ask),
                dec("3"),
                None,
            )
            .unwrap(),
        )
    }

    #[test]
    fn marketable_buy_limit_takes_ask() {
        let decision = LatencySize::default()
            .decide(
                &order(OrderType::Limit, Some("101")),
                &quote("100", "101"),
                None,
                None,
                false,
            )
            .unwrap()
            .unwrap();
        assert_eq!(decision.price, dec("101"));
        assert_eq!(decision.quantity, dec("3"));
        assert_eq!(decision.liquidity, "taker");
    }

    #[test]
    fn optimistic_in_spread_limit_rests_at_limit_when_trade_through_disabled() {
        let mut model = LatencySize::new(0, RemainderPolicy::Rest, false);
        let decision = model
            .decide(
                &order(OrderType::Limit, Some("100.50")),
                &quote("100", "101"),
                None,
                None,
                false,
            )
            .unwrap()
            .unwrap();
        assert_eq!(decision.price, dec("100.50"));
        assert_eq!(decision.liquidity, "maker");
    }

    #[test]
    fn partial_fill_cancels_remainder() {
        let mut model = LatencySize::new(0, RemainderPolicy::Cancel, true);
        model
            .decide(
                &order(OrderType::Market, None),
                &quote("100", "101"),
                None,
                None,
                false,
            )
            .unwrap()
            .unwrap();
        assert!(model.is_cancelled("o1"));
    }
}
