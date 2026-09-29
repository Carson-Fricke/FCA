//! Classification truth table and terminality for session coverage.

use lake_catalog::{BarSource, CoverageRecord, CoverageStatus, MemoryCatalogRepository};
use lake_contracts::{AssetClass, DataType, Date, DateTime};
use lake_coverage::{calculate_coverage, CoverageReport, CoverageState};

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn instant(year: i32, month: u32, day_of_month: u32, hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, minute, 0, 0).expect("valid datetime")
}

fn coverage_record(status: CoverageStatus, session_date: Date) -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        session_date,
        status,
        None,
        0,
        0,
        None,
        None,
        None,
    )
    .expect("valid coverage")
}

fn report(records: impl IntoIterator<Item = CoverageRecord>, as_of: DateTime) -> CoverageReport {
    calculate_coverage(
        &MemoryCatalogRepository::from_records(records),
        None,
        AssetClass::Equity,
        &[DataType::Trade],
        &["AAPL".to_string()],
        None,
        day(2024, 1, 18),
        day(2024, 1, 18),
        as_of,
    )
    .expect("coverage calculation")
}

#[test]
fn closed_sessions_classify_by_the_persisted_status() {
    let session_date = day(2024, 1, 18);
    let as_of = instant(2024, 1, 19, 0, 0);
    let table = [
        (None, CoverageState::Missing),
        (Some(CoverageStatus::Complete), CoverageState::Complete),
        (Some(CoverageStatus::Empty), CoverageState::Empty),
        (Some(CoverageStatus::Partial), CoverageState::Partial),
        (Some(CoverageStatus::Error), CoverageState::Error),
    ];

    for (status, expected) in table {
        let records = status.map_or_else(Vec::new, |status| {
            vec![coverage_record(status, session_date)]
        });
        let report = report(records, as_of);
        let entry = &report.entries[0];
        assert_eq!(entry.state, expected, "status {status:?}");
        assert_eq!(entry.record.is_some(), status.is_some());
        assert_eq!(entry.status(), status);
    }
}

#[test]
fn open_sessions_reclassify_complete_rows_as_replaceable_partials() {
    let session_date = day(2024, 1, 18);
    let report = report(
        [coverage_record(CoverageStatus::Complete, session_date)],
        instant(2024, 1, 18, 15, 0),
    );

    let entry = &report.entries[0];
    assert_eq!(entry.state, CoverageState::Partial);
    assert!(entry.session.is_partial);
    assert!(entry.is_complete());
    assert_eq!(entry.status(), Some(CoverageStatus::Complete));
    assert!(entry.replace_existing());
    assert!(entry.is_actionable());
    assert!(!report.is_satisfied());
}

#[test]
fn open_sessions_without_a_row_or_with_non_empty_rows_are_partial() {
    let as_of = instant(2024, 1, 18, 15, 0);
    for status in [
        None,
        Some(CoverageStatus::Partial),
        Some(CoverageStatus::Error),
    ] {
        let records = status.map_or_else(Vec::new, |status| {
            vec![coverage_record(status, day(2024, 1, 18))]
        });
        let report = report(records, as_of);
        let entry = &report.entries[0];
        assert_eq!(entry.state, CoverageState::Partial, "status {status:?}");
        assert!(entry.is_actionable());
        if status.is_some() {
            assert!(entry.replace_existing());
        } else {
            assert!(!entry.replace_existing());
        }
    }
}

#[test]
fn persisted_empty_wins_even_in_an_open_session() {
    let report = report(
        [coverage_record(CoverageStatus::Empty, day(2024, 1, 18))],
        instant(2024, 1, 18, 15, 0),
    );

    let entry = &report.entries[0];
    assert_eq!(entry.state, CoverageState::Empty);
    assert!(entry.is_terminal());
    assert!(!entry.is_actionable());
    assert!(!entry.replace_existing());
    assert!(report.is_satisfied());
    assert!(report.gap_plan().gaps.is_empty());
}

#[test]
fn closed_partial_and_error_rows_remain_actionable_gaps() {
    let catalog = MemoryCatalogRepository::from_records([
        coverage_record(CoverageStatus::Partial, day(2024, 1, 18)),
        coverage_record(CoverageStatus::Error, day(2024, 1, 19)),
    ]);
    let report = calculate_coverage(
        &catalog,
        None,
        AssetClass::Equity,
        &[DataType::Trade],
        &["AAPL".to_string()],
        None,
        day(2024, 1, 18),
        day(2024, 1, 19),
        instant(2024, 1, 22, 12, 0),
    )
    .expect("coverage calculation");

    assert_eq!(report.entries.len(), 2);
    assert!(report.partial()[0].is_actionable());
    assert!(report.errors()[0].is_actionable());
    assert!(!report.is_satisfied());

    let plan = report.gap_plan();
    assert_eq!(plan.gaps.len(), 2);
    assert_eq!(plan.partial().len(), 1);
    assert_eq!(plan.errors().len(), 1);
    assert!(plan.missing().is_empty());
}

#[test]
fn report_state_accessors_partition_the_entries() {
    let catalog = MemoryCatalogRepository::from_records([
        coverage_record(CoverageStatus::Complete, day(2024, 1, 16)),
        coverage_record(CoverageStatus::Empty, day(2024, 1, 17)),
        coverage_record(CoverageStatus::Partial, day(2024, 1, 18)),
        coverage_record(CoverageStatus::Error, day(2024, 1, 19)),
    ]);
    let report = calculate_coverage(
        &catalog,
        None,
        AssetClass::Equity,
        &[DataType::Trade],
        &["AAPL".to_string()],
        None,
        day(2024, 1, 16),
        day(2024, 1, 22),
        instant(2024, 1, 23, 0, 0),
    )
    .expect("coverage calculation");

    assert_eq!(report.entries.len(), 5);
    assert_eq!(report.complete().len(), 1);
    assert_eq!(report.empty().len(), 1);
    assert_eq!(report.partial().len(), 1);
    assert_eq!(report.errors().len(), 1);
    assert_eq!(report.missing().len(), 1);
    assert!(report.delisted().is_empty());
    assert_eq!(
        report.by_state(CoverageState::Complete)[0].session_date(),
        day(2024, 1, 16)
    );

    let plan = report.gap_plan();
    assert_eq!(plan.gaps.len(), 3);
    assert_eq!(plan.missing().len(), 1);
    assert_eq!(plan.partial().len(), 1);
    assert_eq!(plan.errors().len(), 1);
    assert_eq!(plan.asset_class, AssetClass::Equity);
    assert_eq!(plan.start_date, day(2024, 1, 16));
    assert_eq!(plan.end_date, day(2024, 1, 22));
}

#[test]
fn coverage_state_values_round_trip_and_display() {
    for state in [
        CoverageState::Missing,
        CoverageState::Empty,
        CoverageState::Partial,
        CoverageState::Complete,
        CoverageState::Error,
        CoverageState::Delisted,
    ] {
        assert_eq!(CoverageState::from_value(state.value()), Some(state));
        assert_eq!(state.to_string(), state.value());
    }
    assert_eq!(CoverageState::Delisted.value(), "delisted");
    assert_eq!(CoverageState::from_value("unknown"), None);
}

/// One bars coverage row with optional provenance.
fn bars_record(
    status: CoverageStatus,
    session_date: Date,
    source: Option<BarSource>,
    revision: Option<&str>,
) -> CoverageRecord {
    let record = CoverageRecord::new(
        AssetClass::Equity,
        DataType::Bars,
        "AAPL",
        session_date,
        status,
        Some(format!(
            "date={}/asset_class=equity/data_type=bars/interval=1min/AAPL.parquet",
            session_date.isoformat()
        )),
        5,
        100,
        None,
        None,
        Some("1min".to_string()),
    )
    .expect("valid bars coverage");
    match (source, revision) {
        (Some(source), Some(revision)) => record
            .with_bar_provenance(source, revision)
            .expect("provenance"),
        _ => record,
    }
}

/// One trade coverage row carrying the partition checksum revision.
fn trade_record(session_date: Date, checksum: Option<&str>) -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        session_date,
        CoverageStatus::Complete,
        Some(format!(
            "date={}/asset_class=equity/data_type=trade/AAPL.parquet",
            session_date.isoformat()
        )),
        5,
        100,
        checksum.map(str::to_string),
        None,
        None,
    )
    .expect("valid trade coverage")
}

/// Classify one bars session against the supplied catalog rows.
fn bars_report(records: Vec<CoverageRecord>, as_of: DateTime) -> CoverageReport {
    calculate_coverage(
        &MemoryCatalogRepository::from_records(records),
        None,
        AssetClass::Equity,
        &[DataType::Bars],
        &["AAPL".to_string()],
        Some("1min"),
        day(2024, 1, 18),
        day(2024, 1, 18),
        as_of,
    )
    .expect("coverage calculation")
}

#[test]
fn derived_bars_are_complete_only_while_the_trade_revision_matches() {
    let session_date = day(2024, 1, 18);
    let as_of = instant(2024, 1, 19, 0, 0);
    let derived = bars_record(
        CoverageStatus::Complete,
        session_date,
        Some(BarSource::Derived),
        Some("trade-rev-1"),
    );

    let current = bars_report(
        vec![
            derived.clone(),
            trade_record(session_date, Some("trade-rev-1")),
        ],
        as_of,
    );
    assert_eq!(current.entries[0].state, CoverageState::Complete);
    assert!(current.is_satisfied());

    let stale = bars_report(
        vec![
            derived.clone(),
            trade_record(session_date, Some("trade-rev-2")),
        ],
        as_of,
    );
    assert_eq!(stale.entries[0].state, CoverageState::Partial);
    assert!(stale.entries[0].is_actionable());
    assert!(!stale.is_satisfied());
    assert_eq!(stale.gap_plan().gaps.len(), 1);

    let missing_trades = bars_report(vec![derived], as_of);
    assert_eq!(missing_trades.entries[0].state, CoverageState::Partial);
}

#[test]
fn provider_and_legacy_bars_are_trusted_as_written() {
    let session_date = day(2024, 1, 18);
    let as_of = instant(2024, 1, 19, 0, 0);

    let provider = bars_record(
        CoverageStatus::Complete,
        session_date,
        Some(BarSource::Provider),
        Some("provider-rev-1"),
    );
    let report = bars_report(
        vec![
            provider.clone(),
            trade_record(session_date, Some("trade-rev-2")),
        ],
        as_of,
    );
    assert_eq!(report.entries[0].state, CoverageState::Complete);
    assert!(report.is_satisfied());

    let legacy = bars_record(CoverageStatus::Complete, session_date, None, None);
    let report = bars_report(vec![legacy, trade_record(session_date, None)], as_of);
    assert_eq!(report.entries[0].state, CoverageState::Complete);
    assert!(report.is_satisfied());
}

#[test]
fn stale_derived_empty_bars_are_repairable() {
    let session_date = day(2024, 1, 18);
    let as_of = instant(2024, 1, 19, 0, 0);
    let empty_derived = bars_record(
        CoverageStatus::Empty,
        session_date,
        Some(BarSource::Derived),
        Some("trade-rev-1"),
    );

    let current = bars_report(
        vec![
            empty_derived.clone(),
            trade_record(session_date, Some("trade-rev-1")),
        ],
        as_of,
    );
    assert_eq!(current.entries[0].state, CoverageState::Empty);
    assert!(current.is_satisfied());

    let stale = bars_report(
        vec![
            empty_derived,
            trade_record(session_date, Some("trade-rev-2")),
        ],
        as_of,
    );
    assert_eq!(stale.entries[0].state, CoverageState::Partial);
    assert!(stale.entries[0].is_actionable());
}

#[test]
fn non_bars_rows_keep_the_plain_status_classification() {
    let session_date = day(2024, 1, 18);
    let report = calculate_coverage(
        &MemoryCatalogRepository::from_records([trade_record(session_date, Some("trade-rev-2"))]),
        None,
        AssetClass::Equity,
        &[DataType::Trade],
        &["AAPL".to_string()],
        None,
        session_date,
        session_date,
        instant(2024, 1, 19, 0, 0),
    )
    .expect("coverage calculation");

    assert_eq!(report.entries.len(), 1);
    assert_eq!(report.entries[0].state, CoverageState::Complete);
}
