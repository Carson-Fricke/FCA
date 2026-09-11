use std::collections::HashMap;

use num::Decimal18;

use super::{intrinsic_value, OptionLifecycle, SettlementKind};
use crate::accounting::Fill;
use crate::orders::Side;
use crate::portfolio::hub::PortfolioHub;
use crate::symbol::{OsiSymbol, Symbol};

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

// 2024-01-15 as epoch days.
const TODAY: i64 = 19_737;

fn lifecycle() -> OptionLifecycle {
    let mut hub = PortfolioHub::new("acct", vec![]).unwrap();
    hub.create_book("book", dec("100000")).unwrap();
    OptionLifecycle::new(hub, TODAY, dec("100"), None, vec![7, 1], dec("0")).unwrap()
}

fn option_fill(symbol: &str, side: Side, quantity: &str, price: &str) -> Fill {
    Fill::new(
        "f1",
        "o1",
        Symbol::new(symbol).unwrap(),
        side,
        dec(quantity),
        dec(price),
        Decimal18::ZERO,
        None,
    )
    .unwrap()
}

#[test]
fn intrinsic_value_matches_call_and_put_payoffs() {
    let call = OsiSymbol::new("AAPL240119C00150000").unwrap();
    assert_eq!(intrinsic_value(&call, dec("155")).unwrap(), dec("5"));
    assert_eq!(intrinsic_value(&call, dec("145")).unwrap(), Decimal18::ZERO);
    let put = OsiSymbol::new("AAPL240119P00150000").unwrap();
    assert_eq!(intrinsic_value(&put, dec("145")).unwrap(), dec("5"));
    assert_eq!(intrinsic_value(&put, dec("155")).unwrap(), Decimal18::ZERO);
}

#[test]
fn long_itm_call_exercises_into_underlying_fill() {
    let mut lifecycle = lifecycle();
    lifecycle
        .apply_fill(
            "book",
            &option_fill("AAPL240115C00150000", Side::Buy, "2", "600"),
        )
        .unwrap();
    let mut spots = HashMap::new();
    spots.insert("AAPL".to_string(), dec("160"));
    let settlements = lifecycle.settle_expired(&spots).unwrap();
    assert_eq!(settlements.len(), 1);
    let settlement = &settlements[0];
    assert_eq!(settlement.kind, SettlementKind::Exercised);
    assert_eq!(settlement.contracts, dec("2"));
    let fill = settlement.underlying_fill.as_ref().unwrap();
    assert_eq!(fill.side, Side::Buy);
    assert_eq!(fill.quantity, dec("200"));
    assert_eq!(fill.price, dec("150"));
    assert!(
        lifecycle
            .hub()
            .book("book")
            .unwrap()
            .position("AAPL")
            .unwrap()
            .quantity
            == dec("200")
    );
    assert!(lifecycle.is_retired("AAPL240115C00150000"));
}

#[test]
fn short_itm_call_is_assigned_and_settles_cash() {
    let mut lifecycle = lifecycle();
    lifecycle
        .apply_fill(
            "book",
            &option_fill("AAPL240115C00150000", Side::Sell, "1", "600"),
        )
        .unwrap();
    let mut spots = HashMap::new();
    spots.insert("AAPL".to_string(), dec("160"));
    let settlements = lifecycle.settle_expired(&spots).unwrap();
    let settlement = &settlements[0];
    assert_eq!(settlement.kind, SettlementKind::Assigned);
    assert_eq!(settlement.contracts, dec("1"));
    assert_eq!(settlement.settlement_value, Decimal18::ZERO);
    assert_eq!(
        settlement.underlying_fill.as_ref().unwrap().side,
        Side::Sell
    );
}

#[test]
fn dte_events_emit_once_per_threshold() {
    let mut lifecycle = lifecycle();
    // Contract expires 2024-01-19, three days after TODAY.
    lifecycle
        .apply_fill(
            "book",
            &option_fill("AAPL240119C00150000", Side::Buy, "1", "600"),
        )
        .unwrap();
    let first = lifecycle.poll_dte_events();
    assert_eq!(first.len(), 1);
    assert_eq!(first[0].threshold, 7);
    assert_eq!(first[0].days_to_expiry, 4);
    let second = lifecycle.poll_dte_events();
    assert!(second.is_empty());
}
