use super::*;
use crate::events::{BarEvent, TradeEvent};
use num::Decimal18;
use std::str::FromStr;

fn instant(hour: i64) -> DateTime {
    DateTime::from_micros(hour * 3_600 * 1_000_000)
}

fn trade(seconds: i64) -> MarketEvent {
    MarketEvent::Trade(TradeEvent::new(
        "AAPL".to_string(),
        DateTime::from_micros(seconds * 1_000_000),
        1,
        Decimal18::from_str("10").unwrap(),
        Decimal18::from_str("1").unwrap(),
    ))
}

#[test]
fn detector_finds_session_scoped_gaps() {
    let calendar: Rc<dyn CoverageCalendar> =
        Rc::new(LakeSessionCalendar::new(AssetClass::Crypto).unwrap());
    let mut detector = LiveGapDetector::new(calendar);
    detector.observe(&trade(0)).expect("trade observes");
    // Two hours of silence inside a UTC day is a gap.
    let gaps = detector
        .detect(instant(2), 3_600 * 1_000_000_000, None)
        .unwrap();
    assert_eq!(gaps.len(), 1);
    assert_eq!(gaps[0].symbol, "AAPL");
    assert!(gaps[0].start < gaps[0].end);
}

#[test]
fn bars_are_not_a_gap_healing_partition() {
    let calendar: Rc<dyn CoverageCalendar> =
        Rc::new(LakeSessionCalendar::new(AssetClass::Crypto).unwrap());
    let mut detector = LiveGapDetector::new(calendar);
    let error = detector
        .observe(&MarketEvent::Bar(BarEvent {
            event_id: "bar-1".to_string(),
            timestamp: DateTime::from_micros(1_000_000),
            symbol: "AAPL".to_string(),
            sequence: 1,
            open: Decimal18::from_str("10").unwrap(),
            high: Decimal18::from_str("11").unwrap(),
            low: Decimal18::from_str("9").unwrap(),
            close: Decimal18::from_str("10.5").unwrap(),
            volume: Decimal18::from_str("100").unwrap(),
        }))
        .expect_err("bars must never be filed as a tick partition");
    assert!(
        matches!(error, DataError::Unsupported(ref message)
            if message.contains("gap healing does not track bars")),
        "expected the explicit unsupported error, got {error:?}"
    );
    assert!(detector.tracked().is_empty());
}
