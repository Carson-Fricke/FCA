use lake_catalog::{CoverageRecord, CoverageStatus};
use lake_contracts::{partition_path, AssetClass, DataType, Date, DateTime};
use lake_coverage::{CoverageReport, CoverageSession, CoverageState, SessionCoverage};
use serde_json::json;

use super::{
    classify_sessions, has_expected_path, incomplete_error, sas_unavailable, validate_request,
};

fn date(month: u32, day: u32) -> Date {
    Date::from_ymd(2024, month, day).expect("valid date")
}

fn session(session_date: Date) -> CoverageSession {
    CoverageSession::new(
        session_date,
        DateTime::from_ymd_hms(2024, 1, 18, 14, 30, 0, 0).expect("valid open"),
        DateTime::from_ymd_hms(2024, 1, 18, 21, 0, 0, 0).expect("valid close"),
        false,
    )
    .expect("valid session")
}

fn request() -> super::FileQueryRequest {
    super::FileQueryRequest {
        asset_class: AssetClass::Equity,
        data_type: DataType::Trade,
        symbol: Some("aapl".to_string()),
        start_date: date(1, 18),
        end_date: date(1, 19),
        ensure: false,
        interval: None,
    }
}

fn record(session_date: Date, object_path: Option<&str>) -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        session_date,
        CoverageStatus::Complete,
        object_path.map(str::to_string),
        3,
        30,
        None,
        None,
        None,
    )
    .expect("valid record")
}

fn entry(
    state: CoverageState,
    session_date: Date,
    record: Option<CoverageRecord>,
) -> SessionCoverage {
    SessionCoverage {
        asset_class: AssetClass::Equity,
        data_type: DataType::Trade,
        symbol: "AAPL".to_string(),
        session: session(session_date),
        state,
        record,
    }
}

fn canonical_entry(session_date: Date) -> SessionCoverage {
    let path = partition_path(
        session_date,
        AssetClass::Equity,
        DataType::Trade,
        None,
        "AAPL",
    )
    .expect("canonical path");
    entry(
        CoverageState::Complete,
        session_date,
        Some(record(session_date, Some(&path))),
    )
}

fn report(entries: Vec<SessionCoverage>) -> CoverageReport {
    CoverageReport {
        asset_class: AssetClass::Equity,
        start_date: date(1, 18),
        end_date: date(1, 19),
        entries,
    }
}

#[test]
fn classify_requires_complete_state_and_canonical_path() {
    let with_bad_path = entry(
        CoverageState::Complete,
        date(1, 18),
        Some(record(date(1, 18), Some("../outside.parquet"))),
    );
    let without_record = entry(CoverageState::Complete, date(1, 18), None);
    let empty = entry(CoverageState::Empty, date(1, 19), None);
    let partial = entry(CoverageState::Partial, date(1, 19), None);
    let delisted = entry(CoverageState::Delisted, date(1, 19), None);
    let missing = entry(CoverageState::Missing, date(1, 19), None);
    let complete = canonical_entry(date(1, 18));

    let (usable, incomplete) = classify_sessions(
        &report(vec![
            complete.clone(),
            with_bad_path.clone(),
            without_record.clone(),
            empty.clone(),
            partial.clone(),
            delisted.clone(),
            missing.clone(),
        ]),
        AssetClass::Equity,
        DataType::Trade,
        None,
        "aapl",
    );

    assert_eq!(usable, vec![complete]);
    assert_eq!(
        incomplete,
        vec![
            with_bad_path,
            without_record,
            empty,
            partial,
            delisted,
            missing
        ]
    );
}

#[test]
fn classify_preserves_report_order() {
    let first = canonical_entry(date(1, 18));
    let second = canonical_entry(date(1, 19));

    let (usable, incomplete) = classify_sessions(
        &report(vec![first.clone(), second.clone()]),
        AssetClass::Equity,
        DataType::Trade,
        None,
        "aapl",
    );

    assert_eq!(usable, vec![first, second]);
    assert!(incomplete.is_empty());
}

#[test]
fn has_expected_path_rejects_a_missing_record_or_path() {
    let session_date = date(1, 18);
    assert!(!has_expected_path(
        &entry(CoverageState::Complete, session_date, None),
        AssetClass::Equity,
        DataType::Trade,
        None,
        "aapl",
    ));
    assert!(!has_expected_path(
        &entry(
            CoverageState::Complete,
            session_date,
            Some(record(session_date, None))
        ),
        AssetClass::Equity,
        DataType::Trade,
        None,
        "aapl",
    ));
    assert!(has_expected_path(
        &canonical_entry(session_date),
        AssetClass::Equity,
        DataType::Trade,
        None,
        "aapl"
    ));
}

#[test]
fn incomplete_error_messages_match_python() {
    let entries = vec![entry(CoverageState::Partial, date(1, 19), None)];

    let without_ensure = incomplete_error(&entries, false);
    assert_eq!(without_ensure.code, "coverage_incomplete");
    assert_eq!(
        without_ensure.message,
        "requested coverage is incomplete; set ensure=true to request coverage"
    );

    let after_ensure = incomplete_error(&entries, true);
    assert_eq!(after_ensure.code, "coverage_incomplete");
    assert_eq!(
        after_ensure.message,
        "coverage remains incomplete after ensure"
    );
}

#[test]
fn incomplete_error_details_use_python_state_values() {
    let error = incomplete_error(
        &[
            entry(CoverageState::Empty, date(1, 18), None),
            entry(CoverageState::Error, date(1, 19), None),
        ],
        false,
    );
    assert_eq!(
        error.details,
        Some(json!({
            "sessions": [
                {"session_date": "2024-01-18", "state": "empty"},
                {"session_date": "2024-01-19", "state": "error"},
            ]
        }))
    );
}

#[test]
fn sas_unavailable_message_matches_python() {
    let error = sas_unavailable();
    assert_eq!(error.code, "sas_unavailable");
    assert_eq!(error.message, "file query SAS issuance is not configured");
    assert_eq!(error.details, None);
}

#[test]
fn validate_request_accepts_equity_and_crypto_symbols() {
    assert_eq!(validate_request(&request()).expect("valid"), "aapl");

    let crypto = super::FileQueryRequest {
        asset_class: AssetClass::Crypto,
        data_type: DataType::Quote,
        symbol: Some("BTC/USD".to_string()),
        start_date: date(1, 19),
        end_date: date(1, 21),
        ensure: false,
        interval: None,
    };
    assert_eq!(validate_request(&crypto).expect("valid"), "BTC/USD");
}

#[test]
fn validate_request_enforces_the_interval_pairing() {
    let bars = |interval: Option<&str>| super::FileQueryRequest {
        data_type: DataType::Bars,
        interval: interval.map(str::to_string),
        ..request()
    };

    let missing = validate_request(&bars(None)).expect_err("bars without interval");
    assert_eq!(missing.code, "validation_error");
    assert_eq!(missing.message, "bars requires an interval");

    let unknown = validate_request(&bars(Some("2h"))).expect_err("unknown token");
    assert_eq!(unknown.code, "validation_error");
    assert_eq!(
        unknown.message,
        "invalid bar interval: \"2h\"; expected one of 1min, 1d"
    );

    let misplaced = validate_request(&super::FileQueryRequest {
        interval: Some("1min".to_string()),
        ..request()
    })
    .expect_err("interval on a non-bars type");
    assert_eq!(misplaced.code, "validation_error");
    assert_eq!(misplaced.message, "interval is supported for bars only");

    assert_eq!(
        validate_request(&bars(Some("1min"))).expect("valid"),
        "aapl"
    );
}

#[test]
fn validate_request_requires_a_symbol() {
    for symbol in [None, Some(String::new()), Some("   ".to_string())] {
        let request = super::FileQueryRequest {
            symbol,
            ..request()
        };
        let error = validate_request(&request).expect_err("rejected");
        assert_eq!(error.code, "validation_error");
        assert_eq!(
            error.message,
            "symbol is required for equity and crypto queries"
        );
    }
}

#[test]
fn validate_request_rejects_unsupported_asset_classes() {
    for asset_class in [AssetClass::Option, AssetClass::Reference] {
        let request = super::FileQueryRequest {
            asset_class,
            ..request()
        };
        let error = validate_request(&request).expect_err("rejected");
        assert_eq!(error.code, "validation_error");
        assert_eq!(error.message, "file queries support equity and crypto only");
    }
}
