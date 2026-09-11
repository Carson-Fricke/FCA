use super::*;
use std::str::FromStr;

fn date(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).unwrap()
}

#[test]
fn listing_activity_respects_as_of() {
    let listed = ListingRecord::new(
        "aapl",
        "aapl",
        date(2020, 1, 1),
        Some(date(2023, 1, 1)),
        ListingType::Ipo,
        Some(DelistingType::Merger),
        Some(Decimal18::from_str("100").unwrap()),
    )
    .unwrap();
    assert!(listed.active_on(date(2022, 1, 1), date(2022, 6, 1)));
    // The delisting is unknown before it arrives, so the listing stays active.
    assert!(listed.active_on(date(2024, 1, 1), date(2022, 6, 1)));
    // Once the delisting has arrived, activity stops on the delisting date.
    assert!(!listed.active_on(date(2024, 1, 1), date(2023, 6, 1)));
    assert!(listed.active_on(date(2023, 1, 1), date(2023, 6, 1)));
}
