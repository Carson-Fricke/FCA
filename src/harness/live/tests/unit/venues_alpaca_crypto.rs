use num::Decimal18;

use super::{is_crypto_pair, AlpacaCryptoFeeSchedule, AlpacaCryptoVenue, CryptoOrderState};
use crate::accounting::Fill;
use crate::events::{MarketEvent, QuoteEvent};
use crate::fills::fees::{CryptoMakerTakerFeeModel, FeeRounding, FeeRoundingMode};
use crate::orders::{OrderStatus, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::symbol::Symbol;
use crate::venues::scripted::VenueOutcome;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn rounding() -> FeeRounding {
    FeeRounding::new(dec("0.01"), FeeRoundingMode::HalfEven).unwrap()
}

fn schedule(maker: &str, taker: &str) -> AlpacaCryptoFeeSchedule {
    AlpacaCryptoFeeSchedule::new("alpaca-crypto", "USD", dec(maker), dec(taker), rounding())
        .unwrap()
}

fn fee_model(maker: &str, taker: &str) -> CryptoMakerTakerFeeModel {
    CryptoMakerTakerFeeModel::new(rounding(), dec(maker), dec(taker)).unwrap()
}

fn fill(quantity: &str, price: &str, liquidity: &str) -> Fill {
    Fill::new(
        "f1",
        "o1",
        Symbol::new("BTC/USD").unwrap(),
        Side::Buy,
        dec(quantity),
        dec(price),
        Decimal18::ZERO,
        Some(liquidity.to_string()),
    )
    .unwrap()
}

fn order(order_id: &str, side: Side, quantity: &str, order_type: OrderType) -> SingleLegOrder {
    SingleLegOrder::new(
        order_id,
        Symbol::new("BTC/USD").unwrap(),
        side,
        dec(quantity),
        order_type,
        TimeInForce::Day,
        (order_type == OrderType::Limit).then(|| dec("100")),
        None,
        None,
    )
    .unwrap()
}

fn quote(bid: &str, ask: &str, quantity: &str) -> MarketEvent {
    MarketEvent::Quote(
        QuoteEvent::new(
            Symbol::new("BTC/USD").unwrap(),
            dec(bid),
            dec(quantity),
            dec(ask),
            dec(quantity),
            None,
        )
        .unwrap(),
    )
}

#[test]
fn crypto_pair_detection() {
    assert!(is_crypto_pair("BTC/USD"));
    assert!(!is_crypto_pair("AAPL"));
    assert!(!is_crypto_pair("BTC/USD/EUR"));
}

#[test]
fn reconciling_matching_fees_succeeds() {
    let mut venue = AlpacaCryptoVenue::new(fee_model("0.10", "0.25"), schedule("0.10", "0.25"));
    let fee = venue.reconcile_fee(&fill("200", "100", "taker")).unwrap();
    assert_eq!(fee, dec("0.50"));
    assert_eq!(venue.fee_ledger().len(), 1);
}

#[test]
fn mismatched_fees_surface_an_error() {
    let mut venue = AlpacaCryptoVenue::new(fee_model("0.10", "0.25"), schedule("0.10", "0.30"));
    assert!(venue.reconcile_fee(&fill("200", "100", "taker")).is_err());
    assert_eq!(venue.fee_ledger().len(), 1);
}

#[test]
fn submit_fills_against_visible_book_as_taker() {
    let mut venue = AlpacaCryptoVenue::new(fee_model("0.10", "0.25"), schedule("0.10", "0.25"));
    venue.on_market(quote("99", "100", "200")).unwrap();
    venue
        .submit(order("c1", Side::Buy, "200", OrderType::Market))
        .unwrap();
    let state = venue.state("c1").unwrap();
    assert_eq!(state.status, OrderStatus::Filled);
    assert_eq!(state.filled_quantity, dec("200"));
    assert_eq!(state.fees_paid, dec("0.50"));
    let fills = venue
        .outcomes()
        .iter()
        .filter(|outcome| matches!(outcome, VenueOutcome::Fill(_)))
        .count();
    assert_eq!(fills, 1);
    let decision = &venue.decisions()[0];
    assert_eq!(decision.liquidity, "taker");
}

#[test]
fn resting_order_fills_as_maker_on_later_event() {
    let mut venue = AlpacaCryptoVenue::new(fee_model("0.10", "0.25"), schedule("0.10", "0.25"));
    venue
        .submit(order("c2", Side::Buy, "200", OrderType::Limit))
        .unwrap();
    assert_eq!(venue.state("c2").unwrap().status, OrderStatus::Accepted);
    venue.on_market(quote("99", "100", "200")).unwrap();
    let state = venue.state("c2").unwrap();
    assert_eq!(state.status, OrderStatus::Filled);
    let decision = &venue.decisions()[0];
    assert_eq!(decision.liquidity, "maker");
    assert_eq!(state.fees_paid, dec("0.20"));
}

#[test]
fn ioc_expires_without_a_touch() {
    let mut venue = AlpacaCryptoVenue::new(fee_model("0.10", "0.25"), schedule("0.10", "0.25"));
    let mut ioc = order("c3", Side::Buy, "1", OrderType::Limit);
    ioc.time_in_force = TimeInForce::Ioc;
    ioc.limit_price = Some(dec("90"));
    venue.submit(ioc).unwrap();
    venue.on_market(quote("99", "100", "2")).unwrap();
    assert_eq!(venue.state("c3").unwrap().status, OrderStatus::Expired);
}

#[test]
fn duplicate_submit_and_replace_keep_identity() {
    let mut venue = AlpacaCryptoVenue::new(fee_model("0.10", "0.25"), schedule("0.10", "0.25"));
    let first = order("c4", Side::Buy, "1", OrderType::Limit);
    venue.submit(first.clone()).unwrap();
    assert!(venue.submit(first).is_err());
    let replacement = order("c5", Side::Buy, "2", OrderType::Limit);
    venue.replace("c4", replacement).unwrap();
    assert_eq!(venue.state("c4").unwrap().status, OrderStatus::Replaced);
    assert_eq!(venue.state("c5").unwrap().status, OrderStatus::Accepted);
    let _: CryptoOrderState = venue.active_orders()[0].clone();
}

#[test]
fn non_crypto_and_stop_orders_are_rejected() {
    let mut venue = AlpacaCryptoVenue::new(fee_model("0.10", "0.25"), schedule("0.10", "0.25"));
    let equity = SingleLegOrder::new(
        "e1",
        Symbol::new("AAPL").unwrap(),
        Side::Buy,
        dec("1"),
        OrderType::Market,
        TimeInForce::Day,
        None,
        None,
        None,
    )
    .unwrap();
    assert!(venue.submit(equity).is_err());
    let stop = SingleLegOrder::new(
        "s1",
        Symbol::new("BTC/USD").unwrap(),
        Side::Buy,
        dec("1"),
        OrderType::Stop,
        TimeInForce::Day,
        None,
        Some(dec("100")),
        None,
    )
    .unwrap();
    assert!(venue.submit(stop).is_err());
}
