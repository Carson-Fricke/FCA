use super::*;
use crate::time::DateTime;
use num::Decimal18;
use std::str::FromStr;

fn dec(value: &str) -> Decimal18 {
    Decimal18::from_str(value).unwrap()
}

fn trade(symbol: &str, seconds: i64, sequence: u64, price: &str, qty: &str) -> TradeEvent {
    TradeEvent::new(
        symbol.to_string(),
        DateTime::from_micros(seconds * 1_000_000),
        sequence,
        dec(price),
        dec(qty),
    )
}

#[test]
fn interval_delta_units() {
    assert_eq!(interval_delta("1s").unwrap(), 1_000_000_000);
    assert_eq!(interval_delta("5min").unwrap(), 300_000_000_000);
    assert_eq!(interval_delta("1h").unwrap(), 3_600_000_000_000);
    assert_eq!(interval_delta("1d").unwrap(), 86_400_000_000_000);
    assert!(interval_delta("0s").is_err());
    assert!(interval_delta("1w").is_err());
    assert!(interval_delta("abc").is_err());
}

#[test]
fn bars_aggregate_exactly() {
    let trades = vec![
        trade("AAPL", 1, 1, "10.00", "2"),
        trade("AAPL", 2, 2, "12.00", "1"),
        trade("AAPL", 61, 3, "9.00", "3"),
    ];
    let bars = build_bars(trades, "1min", Some("AAPL")).unwrap();
    assert_eq!(bars.len(), 2);
    let first = &bars[0];
    assert_eq!(first.open, dec("10.00"));
    assert_eq!(first.high, dec("12.00"));
    assert_eq!(first.low, dec("10.00"));
    assert_eq!(first.close, dec("12.00"));
    assert_eq!(first.volume, dec("3"));
    assert_eq!(first.trade_count, 2);
    assert_eq!(bars[1].trade_count, 1);
    assert_eq!(bars[1].start.to_nanos(), 60_000_000_000);
}

#[test]
fn mismatched_symbol_rejected() {
    let mut aggregator = BarAggregator::new("1min", Some("AAPL")).unwrap();
    aggregator.add(trade("AAPL", 1, 1, "10", "1")).unwrap();
    assert!(aggregator.add(trade("MSFT", 2, 2, "10", "1")).is_err());
}
