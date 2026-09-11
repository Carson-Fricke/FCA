use num::Decimal18;

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
        .decide(
            &order(OrderType::Market, None),
            &quote("100", "101"),
            None,
            None,
            false,
        )
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
