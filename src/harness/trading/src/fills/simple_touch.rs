//! Deterministic top-of-book touch decisions for single-leg orders.

use num::Decimal18;

use crate::error::LiveError;
use crate::error::LiveResult;
use crate::events::MarketEvent;
use crate::fills::decision::FillDecision;
use crate::orders::OrderType;
use crate::orders::Side;
use crate::orders::SingleLegOrder;

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
    pub fn is_triggered(&self, order: &SingleLegOrder, market: &MarketEvent) -> LiveResult<bool> {
        validate(order, market)?;
        if !matches!(order.order_type, OrderType::Stop | OrderType::StopLimit) {
            return Ok(true);
        }
        let Some(stop_price) = order.stop_price else {
            return Err(LiveError::Fill("stop order is missing stop_price".into()));
        };
        let price = match market {
            MarketEvent::Trade(trade) => trade.price,
            MarketEvent::Quote(quote) => {
                if order.side == Side::Buy {
                    quote.ask_price
                } else {
                    quote.bid_price
                }
            }
        };
        Ok(crate::matching::stop_triggered(
            order.side, stop_price, price,
        ))
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
    let (price, quote) = match market {
        MarketEvent::Trade(trade) => (trade.price, false),
        MarketEvent::Quote(quote) => (
            if order.side == Side::Buy {
                quote.ask_price
            } else {
                quote.bid_price
            },
            true,
        ),
    };
    crate::matching::touch(
        order.side,
        order.order_type,
        order.limit_price,
        order.stop_price,
        price,
        quote,
        true,
    )
    .1
    .map(|price| (price, true))
}
