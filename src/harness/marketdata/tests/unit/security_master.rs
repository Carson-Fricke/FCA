use super::*;
use crate::catalog::{ListingType, MemoryListingsCatalog};
use crate::clock::ManualClock;
use std::str::FromStr;

fn date(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).unwrap()
}

fn instant(year: i32, month: u32, day: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day, 12, 0, 0, 0).unwrap()
}

fn listing(entity: &str, symbol: &str, listed: Date) -> ListingRecord {
    ListingRecord::new(
        entity,
        symbol,
        listed,
        None,
        ListingType::Unknown,
        None,
        None,
    )
    .unwrap()
}

#[test]
fn resolve_hides_future_and_heals_renames() {
    let catalog = MemoryListingsCatalog {
        episodes: vec![
            listing("AA", "AAA", date(2020, 1, 1)),
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
    let view = SecurityMasterView::new(
        Box::new(ManualClock::new(instant(2022, 6, 1))),
        Rc::new(catalog),
    );
    let snapshot = view.resolve("AAB", None).unwrap().unwrap();
    assert_eq!(snapshot.entity_id, "AA");
    assert_eq!(snapshot.symbols(), vec!["AAA", "AAB"]);
    assert_eq!(snapshot.symbol(), "AAB");
    assert!(view.is_listed("AAA", None).unwrap());
    // Before the successor episode lists, the symbol is unknown entirely.
    assert!(view
        .resolve("AAB", Some(instant(2020, 6, 1)))
        .unwrap()
        .is_none());
}

#[test]
fn delisting_final_price_requires_known_date() {
    let record = ListingRecord::new(
        "BB",
        "BBB",
        date(2020, 1, 1),
        Some(date(2022, 1, 1)),
        ListingType::Ipo,
        Some(DelistingType::Merger),
        Some(Decimal18::from_str("12.5").unwrap()),
    )
    .unwrap();
    let catalog = MemoryListingsCatalog {
        episodes: vec![record],
    };
    let view = SecurityMasterView::new(
        Box::new(ManualClock::new(instant(2023, 1, 1))),
        Rc::new(catalog),
    );
    let snapshot = view.resolve("BBB", None).unwrap().unwrap();
    assert!(!snapshot.is_listed());
    assert_eq!(snapshot.delisted_date(), Some(date(2022, 1, 1)));
    assert_eq!(
        snapshot.final_price(),
        Some(Decimal18::from_str("12.5").unwrap())
    );
}
