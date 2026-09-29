//! Listings-aware coverage classification parity tests.
//!
//! Ports `tests/lake/coverage/test_listings_coverage.py`: point-in-time
//! listing episodes decide whether an uncovered session is `missing` (still
//! ensureable) or `delisted` (terminal), and persisted catalog rows keep their
//! own classification regardless of listing knowledge.

use std::collections::{BTreeMap, BTreeSet};

use lake_catalog::{
    CatalogRepository, CoverageRecord, CoverageStatus, DelistingType, ListingRecord,
    ListingRepository, ListingType, MemoryCatalogRepository, MemoryListingRepository,
};
use lake_contracts::{AssetClass, DataType, Date, DateTime};
use lake_coverage::{calculate_coverage, CoverageReport, CoverageState, SessionCoverage};

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn instant(year: i32, month: u32, day_of_month: u32, hour: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, 0, 0, 0).expect("valid datetime")
}

fn rename_day() -> Date {
    day(2022, 6, 9)
}

fn episode(
    entity_id: &str,
    symbol: &str,
    listed: Date,
    delisted: Option<Date>,
    listing_type: ListingType,
    delisting_type: Option<DelistingType>,
) -> ListingRecord {
    ListingRecord::new(
        entity_id,
        symbol,
        listed,
        delisted,
        listing_type,
        delisting_type,
        None,
    )
    .expect("valid listing")
}

fn fb() -> ListingRecord {
    episode(
        "META",
        "FB",
        day(2012, 5, 18),
        Some(day(2022, 6, 8)),
        ListingType::Ipo,
        Some(DelistingType::Rename),
    )
}

fn meta() -> ListingRecord {
    episode(
        "META",
        "META",
        rename_day(),
        None,
        ListingType::Rename,
        None,
    )
}

fn later() -> ListingRecord {
    episode(
        "LATE",
        "LATE",
        day(2022, 6, 13),
        None,
        ListingType::Ipo,
        None,
    )
}

fn report(
    symbols: &[&str],
    start_date: Date,
    end_date: Date,
    as_of: DateTime,
    listings: Option<&dyn ListingRepository>,
    catalog: Option<&dyn CatalogRepository>,
) -> CoverageReport {
    let symbols: Vec<String> = symbols.iter().map(|symbol| (*symbol).to_string()).collect();
    let empty_catalog = MemoryCatalogRepository::new();
    calculate_coverage(
        catalog.unwrap_or(&empty_catalog),
        listings,
        AssetClass::Equity,
        &[DataType::Trade],
        &symbols,
        None,
        start_date,
        end_date,
        as_of,
    )
    .expect("coverage calculation")
}

fn default_report(symbols: &[&str], listings: Option<&dyn ListingRepository>) -> CoverageReport {
    report(
        symbols,
        day(2022, 6, 1),
        day(2022, 6, 15),
        instant(2022, 6, 15, 21),
        listings,
        None,
    )
}

fn states(symbol: &str, report: &CoverageReport) -> BTreeMap<Date, CoverageState> {
    report
        .entries
        .iter()
        .filter(|entry| entry.symbol == symbol)
        .map(|entry| (entry.session_date(), entry.state))
        .collect()
}

#[test]
fn post_delisting_sessions_report_delisted_and_pre_delisting_stay_ensureable() {
    let listings = MemoryListingRepository::from_records([fb(), meta()]);
    let report = default_report(&["FB"], Some(&listings));

    let states = states("FB", &report);
    assert_eq!(states[&day(2022, 6, 6)], CoverageState::Missing);
    assert_eq!(states[&day(2022, 6, 7)], CoverageState::Missing);
    assert_eq!(states[&day(2022, 6, 8)], CoverageState::Missing);
    assert_eq!(states[&rename_day()], CoverageState::Delisted);
    assert_eq!(states[&day(2022, 6, 10)], CoverageState::Delisted);
    assert_eq!(states[&day(2022, 6, 15)], CoverageState::Delisted);

    let gaps = report.gap_plan();
    let dates: Vec<Date> = gaps
        .gaps
        .iter()
        .map(SessionCoverage::session_date)
        .collect();
    assert_eq!(
        dates,
        vec![
            day(2022, 6, 1),
            day(2022, 6, 2),
            day(2022, 6, 3),
            day(2022, 6, 6),
            day(2022, 6, 7),
            day(2022, 6, 8),
        ]
    );
}

#[test]
fn rename_chain_produces_no_duplicate_actionable_coverage() {
    let listings = MemoryListingRepository::from_records([fb(), meta()]);
    let fb_report = default_report(&["FB"], Some(&listings));
    let meta_report = default_report(&["META"], Some(&listings));

    let fb_actionable: BTreeSet<Date> = fb_report
        .entries
        .iter()
        .filter(|entry| entry.is_actionable())
        .map(SessionCoverage::session_date)
        .collect();
    let meta_actionable: BTreeSet<Date> = meta_report
        .entries
        .iter()
        .filter(|entry| entry.is_actionable())
        .map(SessionCoverage::session_date)
        .collect();
    let all_dates: BTreeSet<Date> = fb_report
        .entries
        .iter()
        .map(SessionCoverage::session_date)
        .collect();

    assert_eq!(
        fb_actionable,
        BTreeSet::from([
            day(2022, 6, 1),
            day(2022, 6, 2),
            day(2022, 6, 3),
            day(2022, 6, 6),
            day(2022, 6, 7),
            day(2022, 6, 8),
        ])
    );
    assert_eq!(
        meta_actionable,
        BTreeSet::from([
            day(2022, 6, 9),
            day(2022, 6, 10),
            day(2022, 6, 13),
            day(2022, 6, 14),
            day(2022, 6, 15),
        ])
    );
    assert!(fb_actionable.is_disjoint(&meta_actionable));
    assert_eq!(&fb_actionable | &meta_actionable, all_dates);
}

#[test]
fn pre_listing_sessions_are_delisted_once_the_listing_is_known() {
    let listings = MemoryListingRepository::from_records([later()]);
    let report = default_report(&["LATE"], Some(&listings));

    let states = states("LATE", &report);
    assert_eq!(states[&day(2022, 6, 10)], CoverageState::Delisted);
    assert_eq!(states[&day(2022, 6, 13)], CoverageState::Missing);
    assert_eq!(states[&day(2022, 6, 15)], CoverageState::Missing);
}

#[test]
fn future_dated_delisting_is_never_exposed_to_an_earlier_as_of() {
    let pending = episode(
        "PEND",
        "PEND",
        day(2018, 1, 2),
        Some(day(2022, 12, 30)),
        ListingType::Ipo,
        Some(DelistingType::Voluntary),
    );
    let listings = MemoryListingRepository::from_records([pending]);

    let before = states("PEND", &default_report(&["PEND"], Some(&listings)));
    assert!(before
        .values()
        .all(|state| *state == CoverageState::Missing));

    let after = states(
        "PEND",
        &report(
            &["PEND"],
            day(2022, 12, 1),
            day(2023, 1, 5),
            instant(2023, 1, 5, 21),
            Some(&listings),
            None,
        ),
    );
    assert!(!after.is_empty());
    for (session_date, state) in &after {
        if *session_date <= day(2022, 12, 30) {
            assert_eq!(*state, CoverageState::Missing, "{session_date}");
        } else {
            assert_eq!(*state, CoverageState::Delisted, "{session_date}");
        }
    }
}

#[test]
fn without_listings_every_uncovered_session_stays_missing() {
    let report = default_report(&["FB"], None);

    assert!(report
        .entries
        .iter()
        .all(|entry| entry.state == CoverageState::Missing));
    assert!(!report.is_satisfied());
}

#[test]
fn delisted_sessions_are_terminal_and_keep_persisted_rows() {
    let catalog = MemoryCatalogRepository::from_records([CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "FB",
        day(2022, 6, 10),
        CoverageStatus::Complete,
        None,
        0,
        0,
        None,
        None,
        None,
    )
    .expect("valid coverage")]);
    let listings = MemoryListingRepository::from_records([fb(), meta()]);
    let report = report(
        &["FB"],
        rename_day(),
        day(2022, 6, 13),
        instant(2022, 6, 15, 21),
        Some(&listings),
        Some(&catalog),
    );

    let by_date: BTreeMap<Date, &SessionCoverage> = report
        .entries
        .iter()
        .map(|entry| (entry.session_date(), entry))
        .collect();

    assert_eq!(by_date[&day(2022, 6, 10)].state, CoverageState::Complete);
    assert_eq!(by_date[&rename_day()].state, CoverageState::Delisted);
    assert_eq!(by_date[&day(2022, 6, 13)].state, CoverageState::Delisted);
    assert!(report.is_satisfied());
    assert!(report.gap_plan().gaps.is_empty());
    let delisted_dates: Vec<Date> = report
        .delisted()
        .iter()
        .map(|entry| entry.session_date())
        .collect();
    assert_eq!(delisted_dates, vec![rename_day(), day(2022, 6, 13)]);
}

#[test]
fn plan_gaps_honors_listing_knowledge() {
    let listings = MemoryListingRepository::from_records([fb(), meta()]);
    let report = report(
        &["FB"],
        rename_day(),
        day(2022, 6, 15),
        instant(2022, 6, 15, 21),
        Some(&listings),
        None,
    );

    assert!(report.gap_plan().gaps.is_empty());
}

#[test]
fn as_of_before_the_listing_keeps_every_session_ensureable() {
    let listings = MemoryListingRepository::from_records([fb(), meta()]);
    let report = report(
        &["META"],
        day(2022, 6, 1),
        day(2022, 6, 15),
        instant(2020, 1, 2, 21),
        Some(&listings),
        None,
    );

    assert!(report
        .entries
        .iter()
        .all(|entry| entry.state == CoverageState::Missing));
}

#[test]
fn delisted_classification_arrives_with_the_delisting_instant() {
    let listings = MemoryListingRepository::from_records([fb()]);

    let before = states(
        "FB",
        &report(
            &["FB"],
            day(2022, 6, 15),
            day(2022, 6, 15),
            instant(2022, 6, 7, 21),
            Some(&listings),
            None,
        ),
    );
    let after = states(
        "FB",
        &report(
            &["FB"],
            day(2022, 6, 15),
            day(2022, 6, 15),
            instant(2022, 6, 8, 21),
            Some(&listings),
            None,
        ),
    );

    assert_eq!(before[&day(2022, 6, 15)], CoverageState::Missing);
    assert_eq!(after[&day(2022, 6, 15)], CoverageState::Delisted);
}
