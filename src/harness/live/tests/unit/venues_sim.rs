use super::{simple_quote, simple_trade, SimulatedVenue};
use crate::orders::{OrderStatus, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::symbol::Symbol;
use crate::venues::scripted::VenueOutcome;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

use num::Decimal18;

fn market_order() -> SingleLegOrder {
    SingleLegOrder::new(
        "o1",
        Symbol::new("AAPL").unwrap(),
        Side::Buy,
        dec("10"),
        OrderType::Market,
        TimeInForce::Day,
        None,
        None,
        None,
    )
    .unwrap()
}

#[test]
fn market_order_fills_against_latest_quote() {
    let mut venue = SimulatedVenue::new("sim").unwrap();
    venue
        .on_market(simple_quote("AAPL", "100", "101", "3").unwrap())
        .unwrap();
    venue.submit(market_order()).unwrap();
    let state = venue.state("o1").unwrap();
    assert_eq!(state.status, OrderStatus::PartiallyFilled);
    assert_eq!(state.filled_quantity, dec("3"));
    let fills = venue
        .outcomes()
        .iter()
        .filter(|outcome| matches!(outcome, VenueOutcome::Fill(_)))
        .count();
    assert_eq!(fills, 1);
}

#[test]
fn duplicate_submit_is_rejected() {
    let mut venue = SimulatedVenue::new("sim").unwrap();
    venue.submit(market_order()).unwrap();
    assert!(venue.submit(market_order()).is_err());
}

#[test]
fn trade_through_plugin_ignores_quotes() {
    let mut venue = SimulatedVenue::with_plugin(
        "sim",
        Box::new(
            crate::fills::trade_through::TradeThrough::new(
                0,
                Decimal18::ZERO,
                true,
                crate::fills::latency_size::RemainderPolicy::Rest,
            )
            .unwrap(),
        ),
    )
    .unwrap();
    venue
        .on_market(simple_quote("AAPL", "100", "101", "10").unwrap())
        .unwrap();
    venue.submit(market_order()).unwrap();
    assert_eq!(venue.state("o1").unwrap().status, OrderStatus::Accepted);
    venue
        .on_market(simple_trade("AAPL", "100", "10").unwrap())
        .unwrap();
    assert_eq!(venue.state("o1").unwrap().status, OrderStatus::Filled);
}
