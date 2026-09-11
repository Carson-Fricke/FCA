use super::*;
use crate::time::{Date, DateTime};
use num::Decimal18;
use std::str::FromStr;

fn dec(value: &str) -> Decimal18 {
    Decimal18::from_str(value).unwrap()
}

fn instant(seconds: i64) -> DateTime {
    DateTime::from_micros(seconds * 1_000_000)
}

fn trade(symbol: &str, seconds: i64, sequence: u64) -> MarketEvent {
    MarketEvent::Trade(TradeEvent::new(
        symbol.to_string(),
        instant(seconds),
        sequence,
        dec("10"),
        dec("1"),
    ))
}

fn session(day: i32) -> ExchangeSession {
    let date = Date::from_days(day);
    ExchangeSession {
        session_date: date,
        open_time: DateTime::midnight(date),
        close_time: DateTime::midnight(date.add_days(1)),
    }
}

#[test]
fn replay_merges_streams_in_order() {
    let stream = ReplayStream::new(
        "AAPL",
        Some(DataType::Trade),
        vec![trade("AAPL", 1, 1), trade("AAPL", 3, 3)],
    )
    .unwrap();
    let mut feed = LakeReplayFeed::new(vec![session(0)], vec![stream], Vec::new(), None).unwrap();
    let events = feed.events().unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].sequence(), 1);
    assert_eq!(events[1].sequence(), 3);
}

/// Golden fingerprints produced by `harness.data.replay` on the same events.
#[test]
fn replay_fingerprints_match_python_golden() {
    let day = Date::from_ymd(2024, 1, 2).unwrap();
    let session = ExchangeSession {
        session_date: day,
        open_time: DateTime::midnight(day),
        close_time: DateTime::midnight(day.add_days(1)),
    };
    let quote = QuoteEvent {
        event_id: "q-1".to_string(),
        timestamp: DateTime::from_ymd_hms(2024, 1, 2, 0, 0, 0, 0).unwrap(),
        sequence: 1,
        symbol: "AAPL".to_string(),
        bid_price: dec("100.000000000000000000"),
        bid_size: dec("1.000000000000000000"),
        ask_price: dec("101.000000000000000000"),
        ask_size: dec("2.000000000000000000"),
        venue: Some("V".to_string()),
    };
    let trade = TradeEvent {
        event_id: "t-1".to_string(),
        timestamp: DateTime::from_ymd_hms(2024, 1, 2, 0, 0, 1, 500_000).unwrap(),
        sequence: 2,
        symbol: "AAPL".to_string(),
        price: dec("100.500000000000000000"),
        quantity: dec("3.000000000000000000"),
        venue: Some("V".to_string()),
        trade_id: Some("42".to_string()),
    };
    let stream = ReplayStream::new(
        "AAPL",
        None,
        vec![MarketEvent::Quote(quote), MarketEvent::Trade(trade)],
    )
    .unwrap();
    let mut feed = LakeReplayFeed::new(vec![session], vec![stream], Vec::new(), None).unwrap();
    assert_eq!(
        feed.file_fingerprint(),
        "cc00a9100f6bb19947304e4398a16798bbb87e0569a0bbdfd8a90f25842f3fac"
    );
    assert_eq!(
        feed.fingerprints().data_fingerprint(),
        "85a05a272b90951dab3570794ffc08cdf42e02f17632e3aef658adea8fb0a3ba"
    );
    let events = feed.events().unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(
        feed.fingerprints().row_fingerprint(),
        "225bea5cab11acb50cad8c1533dd3783192520923c470f873de3c132a0fe5321"
    );
    assert_eq!(
        feed.fingerprints().data_fingerprint(),
        "71969743317ed52e99382036894c167d75c937613b2e803dbe9a2ae75b7f76e2"
    );
}

#[test]
fn event_frames_match_python_byte_layout() {
    let quote = QuoteEvent {
        event_id: "q-1".to_string(),
        timestamp: DateTime::from_ymd_hms(2024, 1, 2, 0, 0, 0, 0).unwrap(),
        sequence: 1,
        symbol: "AAPL".to_string(),
        bid_price: dec("100.000000000000000000"),
        bid_size: dec("1.000000000000000000"),
        ask_price: dec("101.000000000000000000"),
        ask_size: dec("2.000000000000000000"),
        venue: Some("V".to_string()),
    };
    let frame = event_fingerprint(&MarketEvent::Quote(quote));
    let expected = "000000000000000571756f74650000000000000003712d310000000000000020323032342d30312d30325430303a30303a30302e3030303030302b30303a303000000000000000013100000000000000044141504c00000000000000163130302e3030303030303030303030303030303030300000000000000014312e30303030303030303030303030303030303000000000000000163130312e3030303030303030303030303030303030300000000000000014322e303030303030303030303030303030303030000000000000000156";
    assert_eq!(hex_string(&frame), expected);
}
