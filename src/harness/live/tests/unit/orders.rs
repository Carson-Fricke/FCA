use num::Decimal18;

use super::{OrderLeg, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

#[test]
fn single_leg_validates_price_presence() {
    let symbol = Symbol::new("AAPL").unwrap();
    let market = SingleLegOrder::new(
        "o1",
        symbol.clone(),
        Side::Buy,
        dec("10"),
        OrderType::Market,
        TimeInForce::Day,
        None,
        None,
        None,
    );
    assert!(market.is_ok());
    let bad_limit = SingleLegOrder::new(
        "o1",
        symbol.clone(),
        Side::Buy,
        dec("10"),
        OrderType::Limit,
        TimeInForce::Day,
        None,
        None,
        None,
    );
    assert!(bad_limit.is_err());
    let stop = SingleLegOrder::new(
        "o1",
        symbol,
        Side::Buy,
        dec("10"),
        OrderType::Stop,
        TimeInForce::Day,
        None,
        Some(dec("100")),
        None,
    );
    assert!(stop.is_ok());
}

#[test]
fn order_leg_rejects_non_positive_ratio() {
    let leg = OrderLeg::new(
        Symbol::new("AAPL").unwrap(),
        Side::Buy,
        dec("1"),
        dec("0"),
        None,
    );
    assert!(leg.is_err());
}
