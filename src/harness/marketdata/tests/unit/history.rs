use super::*;
use crate::clock::ManualClock;
use crate::events::{QuoteEvent, TradeEvent};
use num::Decimal18;
use std::str::FromStr;

fn dec(value: &str) -> Decimal18 {
    Decimal18::from_str(value).unwrap()
}

fn instant(seconds: i64) -> DateTime {
    DateTime::from_micros(seconds * 1_000_000)
}

fn trade(symbol: &str, seconds: i64, sequence: u64, price: &str) -> MarketEvent {
    MarketEvent::Trade(TradeEvent::new(
        symbol.to_string(),
        instant(seconds),
        sequence,
        dec(price),
        dec("1"),
    ))
}

fn quote(symbol: &str, seconds: i64, sequence: u64, bid: &str, ask: &str) -> MarketEvent {
    MarketEvent::Quote(QuoteEvent::new(
        symbol.to_string(),
        instant(seconds),
        sequence,
        dec(bid),
        dec(ask),
    ))
}

fn view() -> (ManualClock, HistoricalView) {
    let clock = ManualClock::new(instant(100));
    let mut view = HistoricalView::new(Box::new(ManualClock::new(instant(100))));
    view.subscribe("AAPL", HistorySelector::All, None).unwrap();
    (clock, view)
}

#[test]
fn reads_are_clipped_to_head() {
    let (_clock, mut view) = view();
    view.record(trade("AAPL", 10, 1, "10")).unwrap();
    let err = view.quote("AAPL", Some(instant(200))).unwrap_err();
    assert!(matches!(err, DataError::Lookahead(_)));
}

#[test]
fn latest_and_window_match_python_order() {
    let (_clock, mut view) = view();
    view.record(quote("AAPL", 1, 1, "9.5", "10.5")).unwrap();
    view.record(trade("AAPL", 1, 2, "10")).unwrap();
    view.record(trade("AAPL", 2, 3, "11")).unwrap();
    let last = view.last("AAPL", 2, HistorySelector::All, None).unwrap();
    assert_eq!(last.len(), 2);
    assert_eq!(last[0].event_id(), "AAPL-2");
    assert_eq!(last[1].event_id(), "AAPL-3");
    let window = view
        .window("AAPL", instant(1), instant(2), HistorySelector::All, None)
        .unwrap();
    assert_eq!(window.len(), 2);
}

#[test]
fn aggregate_computes_vwap() {
    let (_clock, mut view) = view();
    view.record(trade("AAPL", 1, 1, "10")).unwrap();
    view.record(trade("AAPL", 2, 2, "20")).unwrap();
    let aggregate = view
        .aggregate("AAPL", instant(0), instant(10), None)
        .unwrap();
    assert_eq!(aggregate.trade_count, 2);
    assert_eq!(aggregate.volume, dec("2"));
    assert_eq!(aggregate.notional, dec("30"));
    assert_eq!(aggregate.vwap, Some(dec("15")));
    assert_eq!(aggregate.high, Some(dec("20")));
    assert_eq!(aggregate.low, Some(dec("10")));
}

#[test]
fn ring_buffer_evicts_oldest() {
    let (_clock, mut view) = view();
    view.subscribe("MSFT", HistorySelector::Trade, Some(2))
        .unwrap();
    for seconds in 1..=3 {
        view.record(trade("MSFT", seconds, seconds as u64, "10"))
            .unwrap();
    }
    assert_eq!(view.retained("MSFT", HistorySelector::Trade).unwrap(), 2);
    assert_eq!(view.dropped("MSFT", HistorySelector::Trade).unwrap(), 1);
}
