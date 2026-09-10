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
pub fn resolve_remaining(
    order: &MarketOrder<'_>,
    opts: &DecideOptions,
) -> Result<i128, Error> {
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
pub fn is_triggered(
    order: &MarketOrder<'_>,
    snap: &MarketEvent<'_>,
) -> Result<bool, Error> {
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

#[cfg(test)]
pub(crate) mod testing {
    use super::{FillDecision, Liquidity, MarketEvent, MarketOrder, OrderType, Side};
    use crate::decimal::{from_int, ONE};
    use crate::market::{QuoteEvent, TimeInForce, TradeEvent};

    /// 2024-01-19T14:30:00Z in epoch nanoseconds, shared by the fill-model
    /// golden vectors.
    pub const STAMP_NS: i128 = 1_705_674_600_000_000_000;
    /// `100` at scale 18.
    pub const HUNDRED: i128 = 100 * ONE;
    /// `101` at scale 18.
    pub const ONE0ONE: i128 = 101 * ONE;
    /// `10` at scale 18.
    pub const TEN: i128 = 10 * ONE;
    /// `300` at scale 18.
    pub const THREE_HUNDRED: i128 = 300 * ONE;

    /// Build a quote snapshot for symbol `AAPL`.
    pub fn make_quote(
        event_id: &'static str,
        bid: i128,
        ask: i128,
        bid_quantity: i128,
        ask_quantity: i128,
    ) -> MarketEvent<'static> {
        MarketEvent::Quote(QuoteEvent {
            event_id,
            timestamp_ns: STAMP_NS,
            sequence: 0,
            symbol: "AAPL",
            bid_price: bid,
            bid_quantity,
            ask_price: ask,
            ask_quantity,
            venue: None,
        })
    }

    /// The default `100/101` quote with ten on each side.
    pub fn default_quote(event_id: &'static str) -> MarketEvent<'static> {
        make_quote(event_id, HUNDRED, ONE0ONE, TEN, TEN)
    }

    /// A quote with `100` displayed on each side.
    pub fn large_quote(event_id: &'static str, bid: i128, ask: i128) -> MarketEvent<'static> {
        make_quote(event_id, bid, ask, HUNDRED, HUNDRED)
    }

    /// Build a trade snapshot for symbol `AAPL`.
    pub fn make_trade(
        event_id: &'static str,
        price: i128,
        quantity: i128,
    ) -> MarketEvent<'static> {
        MarketEvent::Trade(TradeEvent {
            event_id,
            timestamp_ns: STAMP_NS,
            sequence: 0,
            symbol: "AAPL",
            price,
            quantity,
            venue: None,
            trade_id: None,
        })
    }

    /// Build an `AAPL` order with the shared stamp.
    pub fn make_order(
        order_id: &'static str,
        side: Side,
        order_type: OrderType,
        quantity: i128,
        limit_price: Option<i128>,
        stop_price: Option<i128>,
    ) -> MarketOrder<'static> {
        MarketOrder {
            order_id,
            symbol: "AAPL",
            side,
            quantity,
            order_type,
            time_in_force: TimeInForce::Day,
            limit_price,
            stop_price,
            timestamp_ns: STAMP_NS,
            sequence: 0,
        }
    }

    /// Assert a decision's quantity, price, and liquidity.
    pub fn expect_decision(
        decision: Option<FillDecision<'_>>,
        quantity: i128,
        price: i128,
        liquidity: Liquidity,
    ) {
        let decision = decision.expect("expected a fill decision");
        assert_eq!(decision.quantity, quantity);
        assert_eq!(decision.price, price);
        assert_eq!(decision.liquidity, liquidity);
    }

    /// Convenience for the shared `dec.fromInt` helper.
    pub fn dec(value: i64) -> i128 {
        from_int(value)
    }
}

#[cfg(test)]
mod tests {
    use super::testing::{
        dec, default_quote, expect_decision, large_quote, make_order, make_quote, make_trade,
        HUNDRED, ONE0ONE, TEN,
    };
    use super::{DecideOptions, Liquidity, SimpleTouch};
    use crate::market::{OrderType, Side};

    #[test]
    fn market_orders_fill_at_the_correct_side() {
        let plugin = SimpleTouch;
        let buy = plugin
            .decide(
                &make_order("market-buy", Side::Buy, OrderType::Market, TEN, None, None),
                &default_quote("q"),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(buy, TEN, ONE0ONE, Liquidity::Taker);
        let sell = plugin
            .decide(
                &make_order("market-sell", Side::Sell, OrderType::Market, TEN, None, None),
                &default_quote("q"),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(sell, TEN, HUNDRED, Liquidity::Taker);
    }

    #[test]
    fn limits_cross_at_the_touch_and_rest_when_not_crossing() {
        let plugin = SimpleTouch;
        let crossing = plugin
            .decide(
                &make_order("cross-buy", Side::Buy, OrderType::Limit, TEN, Some(ONE0ONE), None),
                &large_quote("crossing", HUNDRED, ONE0ONE),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(crossing, TEN, ONE0ONE, Liquidity::Taker);
        let resting = plugin
            .decide(
                &make_order("rest-buy", Side::Buy, OrderType::Limit, TEN, Some(ONE0ONE), None),
                &large_quote("resting", dec(99), dec(102)),
                &DecideOptions::default(),
            )
            .unwrap();
        assert!(resting.is_none());

        let sell_crossing = plugin
            .decide(
                &make_order("cross-sell", Side::Sell, OrderType::Limit, TEN, Some(HUNDRED), None),
                &large_quote("crossing-sell", HUNDRED, ONE0ONE),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(sell_crossing, TEN, HUNDRED, Liquidity::Taker);
        let sell_resting = plugin
            .decide(
                &make_order("rest-sell", Side::Sell, OrderType::Limit, TEN, Some(HUNDRED), None),
                &large_quote("resting-sell", dec(99), dec(102)),
                &DecideOptions::default(),
            )
            .unwrap();
        assert!(sell_resting.is_none());
    }

    #[test]
    fn partial_quantity_is_bounded_by_displayed_touch_quantity() {
        let plugin = SimpleTouch;
        let decision = plugin
            .decide(
                &make_order("partial", Side::Buy, OrderType::Market, TEN, None, None),
                &make_quote("small-ask", HUNDRED, ONE0ONE, TEN, dec(3)),
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(decision, dec(3), ONE0ONE, Liquidity::Taker);
    }

    #[test]
    fn stop_orders_arm_only_on_the_correct_touch() {
        let plugin = SimpleTouch;
        let buy_stop = make_order("stop-buy", Side::Buy, OrderType::Stop, TEN, None, Some(ONE0ONE));
        assert!(super::is_triggered(&buy_stop, &default_quote("buy-trigger")).unwrap());
        assert!(!super::is_triggered(&buy_stop, &make_quote("buy-rest", HUNDRED, HUNDRED, TEN, TEN)).unwrap());
        assert!(plugin
            .decide(
                &buy_stop,
                &make_quote("buy-rest", HUNDRED, HUNDRED, TEN, TEN),
                &DecideOptions::default()
            )
            .unwrap()
            .is_none());
        assert!(plugin
            .decide(&buy_stop, &default_quote("buy-trigger"), &DecideOptions::default())
            .unwrap()
            .is_some());

        let sell_stop = make_order("stop-sell", Side::Sell, OrderType::Stop, TEN, None, Some(HUNDRED));
        assert!(super::is_triggered(&sell_stop, &make_quote("sell-trigger", HUNDRED, ONE0ONE, TEN, TEN)).unwrap());
        assert!(!super::is_triggered(&sell_stop, &make_quote("sell-rest", ONE0ONE, dec(102), TEN, TEN)).unwrap());
        assert!(plugin
            .decide(
                &sell_stop,
                &make_quote("sell-rest", ONE0ONE, dec(102), TEN, TEN),
                &DecideOptions::default()
            )
            .unwrap()
            .is_none());
        assert!(plugin
            .decide(&sell_stop, &make_quote("sell-trigger", HUNDRED, ONE0ONE, TEN, TEN), &DecideOptions::default())
            .unwrap()
            .is_some());
    }

    #[test]
    fn stop_limit_requires_both_trigger_and_limit_touch() {
        let plugin = SimpleTouch;
        let stop_order = make_order(
            "stop-limit",
            Side::Buy,
            OrderType::StopLimit,
            TEN,
            Some(dec(102)),
            Some(ONE0ONE),
        );
        let decision = plugin
            .decide(&stop_order, &default_quote("trigger"), &DecideOptions::default())
            .unwrap();
        assert!(decision.is_some());
        assert_eq!(decision.unwrap().triggering_market.event_id(), "trigger");

        let no_limit_touch = make_order(
            "stop-limit-rest",
            Side::Buy,
            OrderType::StopLimit,
            TEN,
            Some(HUNDRED + 500_000_000_000_000_000),
            Some(ONE0ONE),
        );
        assert!(plugin
            .decide(&no_limit_touch, &default_quote("armed"), &DecideOptions::default())
            .unwrap()
            .is_none());
    }

    #[test]
    fn trade_snapshots_are_supported_without_float_conversion() {
        let plugin = SimpleTouch;
        let traded = make_trade("trade", HUNDRED + 250_000_000_000_000_000, dec(2));
        let decision = plugin
            .decide(
                &make_order("trade-market", Side::Sell, OrderType::Market, dec(3), None, None),
                &traded,
                &DecideOptions::default(),
            )
            .unwrap();
        expect_decision(decision, dec(2), HUNDRED + 250_000_000_000_000_000, Liquidity::Taker);
    }

    #[test]
    fn a_trade_does_not_qualify_a_resting_limit_touch() {
        let plugin = SimpleTouch;
        let traded = make_trade("trade-limit", dec(99), TEN);
        let decision = plugin
            .decide(
                &make_order("limit-trade", Side::Buy, OrderType::Limit, dec(2), Some(HUNDRED), None),
                &traded,
                &DecideOptions::default(),
            )
            .unwrap();
        assert!(decision.is_none());
    }
}
