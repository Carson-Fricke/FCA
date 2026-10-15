//! Ensure flow parity tests with injected fake launchers.

use lake_catalog::{CoverageRecord, CoverageStatus, MemoryCatalogRepository};
use lake_contracts::{AssetClass, DataType, Date, DateTime};
use lake_coverage::{ensure_job_id, requested_coverage, EnsureSpec};

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn instant(year: i32, month: u32, day_of_month: u32, hour: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, 0, 0, 0).expect("valid datetime")
}

fn as_of() -> DateTime {
    instant(2024, 1, 22, 12)
}

fn spec() -> EnsureSpec {
    EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: DataType::Trade,
        symbol: "AAPL".to_string(),
        start_date: day(2024, 1, 18),
        end_date: day(2024, 1, 19),
        interval: None,
    }
}

#[test]
fn ensure_job_id_is_pinned_to_the_python_digest() {
    assert_eq!(
        ensure_job_id(&spec()).expect("job id"),
        "ensure-8036196a191cc542158b5ff9e51141d1a68e90214dee4838acfb589d296fb87e"
    );
}

#[test]
fn ensure_job_id_is_stable_for_equivalent_requests() {
    let noisy = EnsureSpec {
        symbol: "  aapl  ".to_string(),
        ..spec()
    };

    assert_eq!(
        ensure_job_id(&noisy).expect("job id"),
        ensure_job_id(&spec()).expect("job id")
    );
    assert_eq!(noisy.canonical(), spec());
    assert_ne!(
        ensure_job_id(&EnsureSpec {
            symbol: "MSFT".to_string(),
            ..spec()
        })
        .expect("job id"),
        ensure_job_id(&spec()).expect("job id")
    );
}

#[test]
fn requested_coverage_validates_the_interval_pairing() {
    let catalog = MemoryCatalogRepository::new();
    let bars = |interval: Option<&str>| EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: DataType::Bars,
        symbol: "AAPL".to_string(),
        start_date: day(2024, 1, 18),
        end_date: day(2024, 1, 19),
        interval: interval.map(str::to_string),
    };

    let missing = requested_coverage(&catalog, None, &bars(None), as_of(), "ensure")
        .expect_err("bars without an interval");
    assert_eq!(missing.code, "invalid_interval");
    assert_eq!(missing.to_string(), "bars requires an interval");

    let unknown = requested_coverage(&catalog, None, &bars(Some("5m")), as_of(), "ensure")
        .expect_err("unknown interval token");
    assert_eq!(unknown.code, "invalid_interval");
    assert_eq!(
        unknown.to_string(),
        "invalid bar interval: \"5m\"; expected one of 1min, 1d"
    );

    let misplaced = requested_coverage(
        &catalog,
        None,
        &EnsureSpec {
            data_type: DataType::Trade,
            interval: Some("1min".to_string()),
            ..bars(Some("1min"))
        },
        as_of(),
        "ensure",
    )
    .expect_err("interval on a non-bars type");
    assert_eq!(misplaced.code, "invalid_interval");
    assert_eq!(misplaced.to_string(), "interval is supported for bars only");

    let option_bars = requested_coverage(
        &catalog,
        None,
        &EnsureSpec {
            asset_class: AssetClass::Option,
            data_type: DataType::Bars,
            interval: Some("1d".to_string()),
            ..bars(Some("1d"))
        },
        as_of(),
        "ensure",
    )
    .expect("option bars use XNYS sessions");
    assert_eq!(option_bars.entries.len(), 2);
    assert_eq!(option_bars.missing().len(), 2);

    let report = requested_coverage(&catalog, None, &bars(Some("1min")), as_of(), "ensure")
        .expect("equity bars are supported");
    assert_eq!(report.entries.len(), 2);
    assert!(report.missing().len() == 2);
}

#[test]
fn requested_coverage_keeps_bar_intervals_distinct() {
    let date = day(2024, 1, 18);
    let catalog = MemoryCatalogRepository::from_records([CoverageRecord::new(
        AssetClass::Equity,
        DataType::Bars,
        "AAPL",
        date,
        CoverageStatus::Complete,
        Some(
            "date=2024-01-18/asset_class=equity/data_type=bars/interval=1min/AAPL.parquet"
                .to_string(),
        ),
        3,
        100,
        None,
        None,
        Some("1min".to_string()),
    )
    .expect("bars coverage")]);
    let spec = |interval: &str| EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: DataType::Bars,
        symbol: "AAPL".to_string(),
        start_date: date,
        end_date: date,
        interval: Some(interval.to_string()),
    };

    let minute = requested_coverage(&catalog, None, &spec("1min"), as_of(), "ensure")
        .expect("minute coverage");
    assert!(minute.is_satisfied());
    let day_report =
        requested_coverage(&catalog, None, &spec("1d"), as_of(), "ensure").expect("day coverage");
    assert_eq!(day_report.missing().len(), 1);
    assert!(day_report.entries[0].record.is_none());
}
