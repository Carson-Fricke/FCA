use super::*;
use crate::events::TradeEvent;
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
    detector.observe(&trade(0));
    // Two hours of silence inside a UTC day is a gap.
    let gaps = detector
        .detect(instant(2), 3_600 * 1_000_000_000, None)
        .unwrap();
    assert_eq!(gaps.len(), 1);
    assert_eq!(gaps[0].symbol, "AAPL");
    assert!(gaps[0].start < gaps[0].end);
}
