//! Strict deterministic trade-print fill decisions.

use std::collections::HashSet;

use harness_num::Decimal18;

use crate::error::{LiveError, LiveResult};
use crate::events::MarketEvent;
use crate::fills::decision::FillDecision;
use crate::fills::latency_size::RemainderPolicy;
use crate::fills::simple_touch::{validate, SimpleTouch};
use crate::orders::{OrderType, Side, SingleLegOrder};

const ZERO: Decimal18 = Decimal18::ZERO;

/// Require qualifying trade prints for every executable order.
///
/// Quote events never fill this model, including marketable limits. A buy
/// limit requires a print strictly below its limit by default; a sell limit
/// requires a print strictly above. `minimum_trade_through` adds an exact
/// price distance.
#[derive(Clone, Debug)]
pub struct TradeThrough {
    /// Response latency metadata, in microseconds.
    pub response_latency_us: u64,
    /// Exact minimum price distance from the limit.
    pub minimum_trade_through: Decimal18,
    /// Require strict improvement when `true`.
    pub strict: bool,
    /// Venue-side remainder treatment after a partial fill.
    pub remainder_policy: RemainderPolicy,
    cancelled: HashSet<String>,
}

impl Default for TradeThrough {
    fn default() -> Self {
        Self {
            response_latency_us: 0,
            minimum_trade_through: ZERO,
            strict: true,
            remainder_policy: RemainderPolicy::Rest,
            cancelled: HashSet::new(),
        }
    }
}

impl TradeThrough {
    /// Construct a validated trade-through model.
    pub fn new(
        response_latency_us: u64,
        minimum_trade_through: Decimal18,
        strict: bool,
        remainder_policy: RemainderPolicy,
    ) -> LiveResult<Self> {
        if minimum_trade_through.is_negative() {
            return Err(LiveError::Fill(
                "minimum_trade_through must not be negative".into(),
            ));
        }
        Ok(Self {
            response_latency_us,
            minimum_trade_through,
            strict,
            remainder_policy,
            cancelled: HashSet::new(),
        })
    }

    /// Order identifiers whose remainder this model canceled.
    pub fn cancelled_order_ids(&self) -> &HashSet<String> {
        &self.cancelled
    }

    /// Whether this model has canceled an order's remainder.
    pub fn is_cancelled(&self, order_id: &str) -> bool {
        self.cancelled.contains(order_id)
    }

    /// Return one trade-print-bounded decision, or `None`.
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
        let MarketEvent::Trade(trade) = market else {
            return Ok(None);
        };
        let available = match available_quantity {
            Some(value) if value.is_positive() => value,
            Some(_) => return Ok(None),
            None => trade.quantity,
        };

        if matches!(order.order_type, OrderType::Stop | OrderType::StopLimit)
            && !stop_triggered
            && !SimpleTouch::new().is_triggered(order, market)?
        {
            return Ok(None);
        }
        if matches!(order.order_type, OrderType::Limit | OrderType::StopLimit)
            && !self.qualifies(order, trade.price)?
        {
            return Ok(None);
        }

        let quantity = min(remaining, available);
        if !quantity.is_positive() {
            return Ok(None);
        }
        let liquidity = if matches!(order.order_type, OrderType::Limit | OrderType::StopLimit) {
            "maker"
        } else {
            "taker"
        };
        let decision = FillDecision::new(
            &order.order_id,
            quantity,
            trade.price,
            trade.price,
            market.clone(),
            liquidity,
        )?;
        if quantity < remaining && self.remainder_policy == RemainderPolicy::Cancel {
            self.cancelled.insert(order.order_id.clone());
        }
        Ok(Some(decision))
    }

    fn qualifies(&self, order: &SingleLegOrder, price: Decimal18) -> LiveResult<bool> {
        let Some(limit) = order.limit_price else {
            return Err(LiveError::Fill(
                "limit order is missing limit_price".into(),
            ));
        };
        if order.side == Side::Buy {
            let threshold = limit - self.minimum_trade_through;
            Ok(if self.strict {
                price < threshold
            } else {
                price <= threshold
            })
        } else {
            let threshold = limit + self.minimum_trade_through;
            Ok(if self.strict {
                price > threshold
            } else {
                price >= threshold
            })
        }
    }
}

fn min(left: Decimal18, right: Decimal18) -> Decimal18 {
    if left < right {
        left
    } else {
        right
    }
}

#[cfg(test)]
mod tests {
    use harness_num::Decimal18;

    use super::TradeThrough;
    use crate::events::{MarketEvent, QuoteEvent, TradeEvent};
    use crate::fills::latency_size::RemainderPolicy;
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

    fn trade(price: &str, quantity: &str) -> MarketEvent {
        MarketEvent::Trade(
            TradeEvent::new(
                Symbol::new("AAPL").unwrap(),
                dec(price),
                dec(quantity),
                None,
                None,
            )
            .unwrap(),
        )
    }

    #[test]
    fn quotes_never_fill() {
        let quote = MarketEvent::Quote(
            QuoteEvent::new(
                Symbol::new("AAPL").unwrap(),
                dec("99"),
                dec("5"),
                dec("100"),
                dec("5"),
                None,
            )
            .unwrap(),
        );
        let decision = TradeThrough::default()
            .decide(&order(OrderType::Limit, Some("101")), &quote, None, None, false)
            .unwrap();
        assert!(decision.is_none());
    }

    #[test]
    fn buy_limit_requires_strict_print_below_limit() {
        let mut model = TradeThrough::default();
        let at_limit = model
            .decide(
                &order(OrderType::Limit, Some("100")),
                &trade("100", "5"),
                None,
                None,
                false,
            )
            .unwrap();
        assert!(at_limit.is_none());
        let below = model
            .decide(
                &order(OrderType::Limit, Some("100")),
                &trade("99.99", "5"),
                None,
                None,
                false,
            )
            .unwrap()
            .unwrap();
        assert_eq!(below.liquidity, "maker");
        assert_eq!(below.price, dec("99.99"));
    }

    #[test]
    fn partial_fill_cancels_remainder_when_configured() {
        let mut model =
            TradeThrough::new(0, Decimal18::ZERO, true, RemainderPolicy::Cancel).unwrap();
        model
            .decide(
                &order(OrderType::Limit, Some("100")),
                &trade("99.99", "4"),
                None,
                None,
                false,
            )
            .unwrap()
            .unwrap();
        assert!(model.is_cancelled("o1"));
        let next = model
            .decide(
                &order(OrderType::Limit, Some("100")),
                &trade("99.99", "4"),
                Some(dec("6")),
                None,
                false,
            )
            .unwrap();
        assert!(next.is_none());
    }
}
