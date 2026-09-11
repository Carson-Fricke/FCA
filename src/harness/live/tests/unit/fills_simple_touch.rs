use num::Decimal18;

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
        .decide(
            &order(OrderType::Market, None),
            &quote("101", "3"),
            None,
            None,
            false,
        )
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
