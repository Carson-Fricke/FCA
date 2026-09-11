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
