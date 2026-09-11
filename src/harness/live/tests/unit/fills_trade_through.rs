use num::Decimal18;

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
        .decide(
            &order(OrderType::Limit, Some("101")),
            &quote,
            None,
            None,
            false,
        )
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
    let mut model = TradeThrough::new(0, Decimal18::ZERO, true, RemainderPolicy::Cancel).unwrap();
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
