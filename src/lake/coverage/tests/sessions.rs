//! Session-window parity tests for the equity and crypto calendars.

use lake_contracts::{AssetClass, Date, DateTime};
use lake_coverage::{sessions_for, CoverageSession};

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn instant(year: i32, month: u32, day_of_month: u32, hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, minute, 0, 0).expect("valid datetime")
}

#[test]
fn equity_sessions_use_holidays_and_half_day_bounds() {
    let sessions = sessions_for(
        AssetClass::Equity,
        day(2024, 11, 27),
        day(2024, 12, 2),
        instant(2024, 12, 3, 0, 0),
    )
    .expect("equity sessions");

    let dates: Vec<Date> = sessions
        .iter()
        .map(|session| session.session_date)
        .collect();
    assert_eq!(
        dates,
        vec![day(2024, 11, 27), day(2024, 11, 29), day(2024, 12, 2)]
    );
    assert_eq!(sessions[1].open_time, instant(2024, 11, 29, 14, 30));
    assert_eq!(sessions[1].close_time, instant(2024, 11, 29, 18, 0));
    assert!(sessions.iter().all(|session| !session.is_partial));
}

#[test]
fn crypto_sessions_represent_each_utc_day_and_the_current_partial_day() {
    let sessions = sessions_for(
        AssetClass::Crypto,
        day(2024, 1, 18),
        day(2024, 1, 19),
        instant(2024, 1, 19, 12, 0),
    )
    .expect("crypto sessions");

    assert_eq!(
        sessions
            .iter()
            .map(|session| (session.session_date, session.is_partial))
            .collect::<Vec<_>>(),
        vec![(day(2024, 1, 18), false), (day(2024, 1, 19), true)]
    );
    assert_eq!(sessions[1].open_time, instant(2024, 1, 19, 0, 0));
    assert_eq!(sessions[1].close_time, instant(2024, 1, 20, 0, 0));
}

#[test]
fn sessions_for_rejects_unsupported_asset_classes() {
    let error = sessions_for(
        AssetClass::Option,
        day(2024, 1, 18),
        day(2024, 1, 18),
        instant(2024, 1, 18, 12, 0),
    )
    .expect_err("option sessions are unsupported");

    assert_eq!(error.code, "unsupported_asset_class");
    assert_eq!(
        error.to_string(),
        "coverage planning supports equity and crypto only"
    );
}

#[test]
fn coverage_session_new_rejects_a_reversed_window() {
    let error = CoverageSession::new(
        day(2024, 1, 18),
        instant(2024, 1, 18, 15, 0),
        instant(2024, 1, 18, 14, 0),
        false,
    )
    .expect_err("reversed windows are invalid");

    assert_eq!(error.code, "invalid_session");
    assert_eq!(error.to_string(), "open_time must be before close_time");

    let session = CoverageSession::new(
        day(2024, 1, 18),
        instant(2024, 1, 18, 14, 30),
        instant(2024, 1, 18, 21, 0),
        true,
    )
    .expect("valid session");
    assert!(session.is_partial);
}

#[test]
fn from_exchange_marks_the_window_that_is_currently_trading() {
    let sessions = lake_contracts::xnys_sessions(day(2024, 11, 27), day(2024, 11, 27))
        .expect("exchange sessions");
    let exchange = sessions[0];

    let open = CoverageSession::from_exchange(&exchange, instant(2024, 11, 27, 15, 0));
    let closed = CoverageSession::from_exchange(&exchange, instant(2024, 11, 27, 22, 0));

    assert!(open.is_partial);
    assert!(!closed.is_partial);
    assert_eq!(open.session_date, day(2024, 11, 27));
    assert_eq!(open.close_time, instant(2024, 11, 27, 21, 0));
}
