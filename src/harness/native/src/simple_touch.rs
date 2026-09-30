//! Deterministic top-of-book touch decisions for single-leg orders, ported
//! from `src/harness/fills/simple_touch.py`.
//!
//! Every decision is an exact `i128` fixed-point value at scale 18. Quotes use
//! the ask for buys and the bid for sells; a quote's displayed side quantity
//! bounds a fill, which gives the simulated venue deterministic partial-fill
//! behavior without a random participation model. A trade alone is not a
//! SimpleTouch for a resting limit — print qualification belongs to the
//! trade-through model.

use crate::market::{Error, MarketEvent, MarketOrder, OrderType, Side};

/// Liquidity flag of a fill decision (`FillDecision.liquidity`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Liquidity {
    /// Maker (resting) liquidity.
    Maker,
    /// Taker (aggressive) liquidity.
    Taker,
}

/// Venue-side treatment represented by a plugin after a partial fill.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RemainderPolicy {
    /// Cancel the order remainder after a partial fill.
    Cancel,
    /// Let the remainder rest.
    Rest,
}

/// One exact fill decision and the immutable market event that caused it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FillDecision<'a> {
    /// Order identifier.
    pub order_id: &'a str,
    /// Filled quantity (unscaled scale 18).
    pub quantity: i128,
    /// Execution price (unscaled scale 18).
    pub price: i128,
    /// Decision price (unscaled scale 18).
    pub decision_price: i128,
    /// The immutable market event that triggered the decision.
    pub triggering_market: MarketEvent<'a>,
    /// Liquidity flag.
    pub liquidity: Liquidity,
}

impl FillDecision<'_> {
    /// Validate the decision against the Python record contract.
    pub fn validate(&self) -> Result<(), Error> {
        if self.order_id.is_empty() {
            return Err(Error::InvalidArgument);
        }
        if self.quantity <= 0 {
            return Err(Error::InvalidArgument);
        }
        if self.price <= 0 || self.decision_price <= 0 {
            return Err(Error::InvalidArgument);
        }
        self.triggering_market.validate()
    }

    /// The execution price under the common fill vocabulary.
    pub fn fill_price(&self) -> i128 {
        self.price
    }
}

/// Keyword-style options shared by every fill model's `decide`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct DecideOptions {
    /// Authoritative remaining quantity, overriding the order quantity.
    pub remaining_quantity: Option<i128>,
    /// Explicit displayed/available quantity.
    pub available_quantity: Option<i128>,
    /// Whether a stop order has already been armed.
    pub stop_triggered: bool,
}

/// Validate an order/snapshot pair shares a symbol and both records validate.
pub fn validate_pair(order: &MarketOrder<'_>, snap: &MarketEvent<'_>) -> Result<(), Error> {
    order.validate()?;
    snap.validate()?;
    if order.symbol != snap.symbol() {
        return Err(Error::InvalidArgument);
    }
    Ok(())
}

/// Resolve the authoritative remaining quantity for a decision.
pub fn resolve_remaining(order: &MarketOrder<'_>, opts: &DecideOptions) -> Result<i128, Error> {
    let remaining = opts.remaining_quantity.unwrap_or(order.quantity);
    if remaining <= 0 {
        return Err(Error::InvalidArgument);
    }
    Ok(remaining)
}

/// The displayed quantity on the touching side of the snapshot.
pub fn displayed_quantity(side: Side, snap: &MarketEvent<'_>) -> i128 {
    match snap {
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

/// Whether a stop order is armed by this market snapshot.
pub fn is_triggered(order: &MarketOrder<'_>, snap: &MarketEvent<'_>) -> Result<bool, Error> {
    if matches!(order.order_type, OrderType::Market | OrderType::Limit) {
        return Ok(true);
    }
    let stop_price = order.stop_price.ok_or(Error::InvalidArgument)?;
    Ok(match snap {
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

/// Fill a single-leg order at the first visible quote or trade touch.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SimpleTouch;

impl SimpleTouch {
    /// Decide a fill for `order` against `snap`.
    pub fn decide<'a>(
        &self,
        order: &MarketOrder<'a>,
        snap: &MarketEvent<'a>,
        opts: &DecideOptions,
    ) -> Result<Option<FillDecision<'a>>, Error> {
        validate_pair(order, snap)?;
        let remaining = resolve_remaining(order, opts)?;

        let available = match opts.available_quantity {
            Some(explicit) => {
                if explicit <= 0 {
                    return Ok(None);
                }
                explicit
            }
            None => displayed_quantity(order.side, snap),
        };

        if matches!(order.order_type, OrderType::Stop | OrderType::StopLimit)
            && !opts.stop_triggered
            && !is_triggered(order, snap)?
        {
            return Ok(None);
        }

        let Some((price, touched)) = touch_price(order, snap)? else {
            return Ok(None);
        };
        if !touched {
            return Ok(None);
        }

        let quantity = remaining.min(available);
        if quantity <= 0 {
            return Ok(None);
        }
        Ok(Some(FillDecision {
            order_id: order.order_id,
            quantity,
            price,
            decision_price: price,
            triggering_market: *snap,
            liquidity: Liquidity::Taker,
        }))
    }
}

/// The touch price and whether the order's price crossed it.
fn touch_price(
    order: &MarketOrder<'_>,
    snap: &MarketEvent<'_>,
) -> Result<Option<(i128, bool)>, Error> {
    let result = match snap {
        MarketEvent::Trade(trade) => match order.order_type {
            OrderType::Market | OrderType::Stop => (trade.price, true),
            _ => (trade.price, false),
        },
        MarketEvent::Quote(quote) => {
            if order.side == Side::Buy {
                match order.order_type {
                    OrderType::Market | OrderType::Stop => (quote.ask_price, true),
                    _ => {
                        let limit = order.limit_price.ok_or(Error::InvalidArgument)?;
                        (quote.ask_price, quote.ask_price <= limit)
                    }
                }
            } else {
                match order.order_type {
                    OrderType::Market | OrderType::Stop => (quote.bid_price, true),
                    _ => {
                        let limit = order.limit_price.ok_or(Error::InvalidArgument)?;
                        (quote.bid_price, quote.bid_price >= limit)
                    }
                }
            }
        }
    };
    Ok(Some(result))
}
