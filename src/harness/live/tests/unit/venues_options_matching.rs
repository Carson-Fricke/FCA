use num::Decimal18;

use super::OptionsMatchingEngine;
use crate::events::QuoteEvent;
use crate::fills::mid_touch::MidTouch;
use crate::oms::groups::GroupOrderType;
use crate::orders::{MultiLegOrder, OrderLeg, OrderType, Side, TimeInForce};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn leg(symbol: &str, side: Side) -> OrderLeg {
    OrderLeg::new(Symbol::new(symbol).unwrap(), side, dec("1"), dec("1"), None).unwrap()
}

fn spread(order_id: &str, order_type: OrderType, limit: Option<&str>) -> MultiLegOrder {
    MultiLegOrder::new(
        order_id,
        vec![leg("AAPL", Side::Buy), leg("MSFT", Side::Sell)],
        order_type,
        TimeInForce::Day,
        None,
        limit.map(dec),
    )
    .unwrap()
}

fn quote(symbol: &str, bid: &str, ask: &str, qty: &str) -> QuoteEvent {
    QuoteEvent::new(
        Symbol::new(symbol).unwrap(),
        dec(bid),
        dec(qty),
        dec(ask),
        dec(qty),
        None,
    )
    .unwrap()
}

#[test]
fn market_group_fills_atomically_when_all_legs_quoted() {
    let mut engine = OptionsMatchingEngine::new("options", MidTouch::default()).unwrap();
    engine.on_quote(quote("AAPL", "100", "101", "5")).unwrap();
    engine
        .submit(spread("g1", OrderType::Market, None), None)
        .unwrap();
    assert_eq!(
        engine.state("g1").unwrap().status,
        crate::oms::groups::GroupStatus::Accepted
    );
    engine.on_quote(quote("MSFT", "99", "100", "5")).unwrap();
    let snapshot = engine.state("g1").unwrap();
    assert_eq!(snapshot.status, crate::oms::groups::GroupStatus::Filled);
    assert_eq!(engine.fills_for("g1").unwrap().len(), 2);
}

#[test]
fn ioc_group_expires_without_a_complete_cross() {
    let mut engine = OptionsMatchingEngine::new("options", MidTouch::default()).unwrap();
    let order = MultiLegOrder::new(
        "g2",
        vec![leg("AAPL", Side::Buy), leg("MSFT", Side::Sell)],
        OrderType::Market,
        TimeInForce::Ioc,
        None,
        None,
    )
    .unwrap();
    engine.submit(order, None).unwrap();
    assert_eq!(
        engine.state("g2").unwrap().status,
        crate::oms::groups::GroupStatus::Expired
    );
}

#[test]
fn net_debit_limit_requires_net_debit() {
    let mut engine = OptionsMatchingEngine::new("options", MidTouch::default()).unwrap();
    engine
        .submit(
            spread("g3", OrderType::Limit, Some("3")),
            Some(GroupOrderType::NetDebit),
        )
        .unwrap();
    engine.on_quote(quote("AAPL", "101", "102", "5")).unwrap();
    engine.on_quote(quote("MSFT", "99", "100", "5")).unwrap();
    let snapshot = engine.state("g3").unwrap();
    assert_eq!(snapshot.status, crate::oms::groups::GroupStatus::Filled);
    assert_eq!(snapshot.net_price.unwrap(), dec("2"));
}
