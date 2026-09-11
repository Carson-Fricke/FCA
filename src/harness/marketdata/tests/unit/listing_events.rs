use super::*;
use crate::catalog::{ListingRecord, MemoryListingsCatalog};
use crate::clock::ManualClock;
use std::rc::Rc;
use std::str::FromStr;

fn date(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).unwrap()
}

fn instant(year: i32, month: u32, day: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day, 12, 0, 0, 0).unwrap()
}

#[test]
fn rename_and_delisting_are_due_on_their_dates() {
    let catalog = MemoryListingsCatalog {
        episodes: vec![
            ListingRecord::new(
                "AA",
                "AAA",
                date(2020, 1, 1),
                Some(date(2021, 1, 1)),
                ListingType::Ipo,
                Some(DelistingType::Rename),
                None,
            )
            .unwrap(),
            ListingRecord::new(
                "AA",
                "AAB",
                date(2021, 1, 1),
                None,
                ListingType::Rename,
                None,
                None,
            )
            .unwrap(),
        ],
    };
    let mut deriver = ListingEventDeriver::new(
        Box::new(ManualClock::new(instant(2021, 6, 1))),
        Rc::new(catalog),
    );
    let events = deriver.poll(None).unwrap();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].kind(), CorporateActionKind::Rename);
    match &events[0] {
        CorporateActionEvent::Rename {
            old_symbol,
            new_symbol,
            ..
        } => {
            assert_eq!(old_symbol, "AAA");
            assert_eq!(new_symbol, "AAB");
        }
        _ => panic!("expected rename"),
    }
    // Polling again is idempotent.
    assert!(deriver.poll(None).unwrap().is_empty());
}

#[test]
fn pessimistic_settlement_is_short_hostile() {
    let long =
        PessimisticDelistingSettlement::settlement_value(Decimal18::from_str("10").unwrap(), None)
            .unwrap();
    assert_eq!(long, Decimal18::ZERO);
    let short =
        PessimisticDelistingSettlement::settlement_value(Decimal18::from_str("-10").unwrap(), None);
    assert!(short.is_err());
    let priced = PessimisticDelistingSettlement::settlement_value(
        Decimal18::from_str("10").unwrap(),
        Some(Decimal18::from_str("2").unwrap()),
    )
    .unwrap();
    assert_eq!(priced, Decimal18::from_str("20").unwrap());
}
