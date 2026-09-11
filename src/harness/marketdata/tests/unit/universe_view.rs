use super::*;
use crate::catalog::{ListingType, MemoryListingsCatalog};
use crate::clock::ManualClock;

fn date(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).unwrap()
}

fn instant(year: i32, month: u32, day: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day, 12, 0, 0, 0).unwrap()
}

#[test]
fn equity_universe_collapses_renames() {
    let catalog = MemoryListingsCatalog {
        episodes: vec![
            ListingRecord::new(
                "AA",
                "AAA",
                date(2020, 1, 1),
                Some(date(2021, 1, 1)),
                ListingType::Ipo,
                Some(crate::catalog::DelistingType::Rename),
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
    let source: Rc<dyn UniverseSource> = Rc::new(EquityListingUniverse::new(Rc::new(catalog)));
    let view = UniverseView::new(
        Box::new(ManualClock::new(instant(2022, 1, 1))),
        vec![source],
        UniverseComposition::Union,
    )
    .unwrap();
    assert_eq!(view.symbols(None).unwrap(), vec!["AAB"]);
    assert!(!view.membership("AAA", None).unwrap());
}
