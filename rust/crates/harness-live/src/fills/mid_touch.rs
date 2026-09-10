//! Pessimistic midpoint fill decisions for quoted option markets.
//!
//! A limit order fills at the exact quote midpoint `M = (bid + ask) / 2` only
//! when the adverse touch is marketable through the limit (default), the
//! midpoint is an exact multiple of `tick_size`, and the spread does not
//! exceed the optional `maximum_spread`. Setting `require_adverse_touch=false`
//! selects the explicitly optimistic mid-cross trigger.

use harness_num::{Decimal18, RoundingMode};

use crate::error::{LiveError, LiveResult};
use crate::events::MarketEvent;
use crate::fills::decision::FillDecision;
use crate::fills::simple_touch::{validate, SimpleTouch};
use crate::orders::{OrderType, Side, SingleLegOrder};

/// Adverse-midpoint fill model for options.
#[derive(Clone, Debug)]
pub struct MidTouch {
    /// Response latency metadata, in microseconds.
    pub response_latency_us: u64,
    /// Smallest valid midpoint increment.
    pub tick_size: Decimal18,
    /// Optional quote-spread gate.
    pub maximum_spread: Option<Decimal18>,
    /// Select the documented pessimistic rule when `true`.
    pub require_adverse_touch: bool,
}

impl Default for MidTouch {
    fn default() -> Self {
        Self {
            response_latency_us: 0,
            tick_size: "0.01".parse().expect("valid default tick"),
            maximum_spread: None,
            require_adverse_touch: true,
        }
    }
}

impl MidTouch {
    /// Construct a validated midpoint model.
    pub fn new(
        response_latency_us: u64,
        tick_size: Decimal18,
        maximum_spread: Option<Decimal18>,
        require_adverse_touch: bool,
    ) -> LiveResult<Self> {
        if !tick_size.is_positive() {
            return Err(LiveError::Fill("tick_size must be positive".into()));
        }
        if let Some(spread) = maximum_spread {
            if spread.is_negative() {
                return Err(LiveError::Fill(
                    "maximum_spread must not be negative".into(),
                ));
            }
        }
        Ok(Self {
            response_latency_us,
            tick_size,
            maximum_spread,
            require_adverse_touch,
        })
    }

    /// Return whether a stop order is armed by this market snapshot.
    pub fn is_triggered(
        &self,
        order: &SingleLegOrder,
        market: &MarketEvent,
    ) -> LiveResult<bool> {
        SimpleTouch::new().is_triggered(order, market)
    }

    /// Return one adverse-midpoint decision, or `None`.
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
        if matches!(order.order_type, OrderType::Stop | OrderType::StopLimit)
            && !stop_triggered
            && !self.is_triggered(order, market)?
        {
            return Ok(None);
        }
        match market {
            MarketEvent::Trade(trade) => {
                self.decide_trade(order, trade, remaining, available_quantity)
            }
            MarketEvent::Quote(quote) => {
                self.decide_quote(order, quote, remaining, available_quantity)
            }
        }
    }

    /// Return the fillable exact midpoint for a quote, or `None`.
    pub fn mid_execution(
        &self,
        quote: &crate::events::QuoteEvent,
    ) -> LiveResult<Option<Decimal18>> {
        let spread = quote.ask_price - quote.bid_price;
        if let Some(maximum) = self.maximum_spread {
            if spread > maximum {
                return Ok(None);
            }
        }
        let midpoint = (quote.bid_price + quote.ask_price) / Decimal18::from_i64(2);
        if !is_multiple(midpoint, self.tick_size) {
            return Ok(None);
        }
        Ok(Some(midpoint))
    }

    fn decide_trade(
        &self,
        order: &SingleLegOrder,
        trade: &crate::events::TradeEvent,
        remaining: Decimal18,
        available_quantity: Option<Decimal18>,
    ) -> LiveResult<Option<FillDecision>> {
        if matches!(order.order_type, OrderType::Limit | OrderType::StopLimit) {
            return Ok(None);
        }
        let available = match available_quantity {
            Some(value) if value.is_positive() => value,
            Some(_) => return Ok(None),
            None => trade.quantity,
        };
        let quantity = min(remaining, available);
        if !quantity.is_positive() {
            return Ok(None);
        }
        Ok(Some(FillDecision::new(
            &order.order_id,
            quantity,
            trade.price,
            trade.price,
            MarketEvent::Trade(trade.clone()),
            "taker",
        )?))
    }

    fn decide_quote(
        &self,
        order: &SingleLegOrder,
        quote: &crate::events::QuoteEvent,
        remaining: Decimal18,
        available_quantity: Option<Decimal18>,
    ) -> LiveResult<Option<FillDecision>> {
        if matches!(order.order_type, OrderType::Market | OrderType::Stop) {
            let (price, displayed) = if order.side == Side::Buy {
                (quote.ask_price, quote.ask_quantity)
            } else {
                (quote.bid_price, quote.bid_quantity)
            };
            let available = match available_quantity {
                Some(value) if value.is_positive() => value,
                Some(_) => return Ok(None),
                None => displayed,
            };
            let quantity = min(remaining, available);
            if !quantity.is_positive() {
                return Ok(None);
            }
            return Ok(Some(FillDecision::new(
                &order.order_id,
                quantity,
                price,
                price,
                MarketEvent::Quote(quote.clone()),
                "taker",
            )?));
        }

        let Some(limit) = order.limit_price else {
            return Err(LiveError::Fill(
                "limit order is missing limit_price".into(),
            ));
        };
        let Some(midpoint) = self.mid_execution(quote)? else {
            return Ok(None);
        };
        let displayed = if order.side == Side::Buy {
            if self.require_adverse_touch {
                if quote.ask_price > limit {
                    return Ok(None);
                }
            } else if midpoint > limit {
                return Ok(None);
            }
            quote.ask_quantity
        } else {
            if self.require_adverse_touch {
                if quote.bid_price < limit {
                    return Ok(None);
                }
            } else if midpoint < limit {
                return Ok(None);
            }
            quote.bid_quantity
        };
        let available = match available_quantity {
            Some(value) if value.is_positive() => value,
            Some(_) => return Ok(None),
            None => displayed,
        };
        let quantity = min(remaining, available);
        if !quantity.is_positive() {
            return Ok(None);
        }
        Ok(Some(FillDecision::new(
            &order.order_id,
            quantity,
            midpoint,
            midpoint,
            MarketEvent::Quote(quote.clone()),
            "maker",
        )?))
    }
}

fn min(left: Decimal18, right: Decimal18) -> Decimal18 {
    if left < right {
        left
    } else {
        right
    }
}

fn is_multiple(value: Decimal18, tick: Decimal18) -> bool {
    if !tick.is_positive() {
        return false;
    }
    let quotient = value / tick;
    quotient == quotient.round_to(RoundingMode::Truncate, 0).unwrap_or(quotient)
}

#[cfg(test)]
mod tests {
    use harness_num::Decimal18;

    use super::MidTouch;
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
                dec("5"),
                None,
            )
            .unwrap(),
        )
    }

    #[test]
    fn limit_fills_at_midpoint_when_adverse_touch_marketable() {
        let decision = MidTouch::default()
            .decide(
                &order(OrderType::Limit, Some("101")),
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
    fn off_tick_midpoint_is_rejected() {
        let decision = MidTouch::default()
            .decide(
                &order(OrderType::Limit, Some("101")),
                &quote("100", "101.01"),
                None,
                None,
                false,
            )
            .unwrap();
        assert!(decision.is_none());
    }

    #[test]
    fn market_order_fills_at_far_touch() {
        let decision = MidTouch::default()
            .decide(&order(OrderType::Market, None), &quote("100", "101"), None, None, false)
            .unwrap()
            .unwrap();
        assert_eq!(decision.price, dec("101"));
        assert_eq!(decision.liquidity, "taker");
    }

    #[test]
    fn adverse_touch_gate_blocks_far_touch_beyond_limit() {
        let decision = MidTouch::default()
            .decide(
                &order(OrderType::Limit, Some("100.25")),
                &quote("100", "101"),
                None,
                None,
                false,
            )
            .unwrap();
        assert!(decision.is_none());
    }
}
