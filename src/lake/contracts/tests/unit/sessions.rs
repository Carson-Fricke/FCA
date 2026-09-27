use super::*;

fn date(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).expect("valid date")
}

#[test]
fn xnys_sessions_skip_weekends_and_holidays() {
    let sessions = xnys_sessions(date(2024, 1, 12), date(2024, 1, 17)).expect("sessions");
    let days: Vec<String> = sessions
        .iter()
        .map(|session| session.session_date.isoformat())
        .collect();
    assert_eq!(days, vec!["2024-01-12", "2024-01-16", "2024-01-17",]);
}

#[test]
fn xnys_sessions_carry_dst_aware_bounds() {
    let summer = xnys_sessions(date(2024, 6, 3), date(2024, 6, 3)).expect("summer");
    assert_eq!(
        summer[0].open_time.isoformat_seconds_z(),
        "2024-06-03T13:30:00Z"
    );
    assert_eq!(
        summer[0].close_time.isoformat_seconds_z(),
        "2024-06-03T20:00:00Z"
    );
    let winter = xnys_sessions(date(2024, 11, 27), date(2024, 11, 27)).expect("winter");
    assert_eq!(
        winter[0].open_time.isoformat_seconds_z(),
        "2024-11-27T14:30:00Z"
    );
    assert_eq!(
        winter[0].close_time.isoformat_seconds_z(),
        "2024-11-27T21:00:00Z"
    );
}

#[test]
fn xnys_sessions_apply_early_closes() {
    let cases = [
        (date(2024, 7, 3), "2024-07-03T17:00:00Z"),
        (date(2024, 11, 29), "2024-11-29T18:00:00Z"),
        (date(2024, 12, 24), "2024-12-24T18:00:00Z"),
    ];
    for (day, expected_close) in cases {
        let sessions = xnys_sessions(day, day).expect("session");
        assert_eq!(sessions.len(), 1, "expected one session on {day}");
        assert_eq!(sessions[0].close_time.isoformat_seconds_z(), expected_close);
    }
}

#[test]
fn xnys_sessions_apply_adhoc_closures() {
    for day in [date(2018, 12, 5), date(2025, 1, 9)] {
        let sessions = xnys_sessions(day, day).expect("sessions");
        assert!(sessions.is_empty(), "expected no session on {day}");
    }
}

#[test]
fn crypto_sessions_are_utc_days() {
    let sessions = crypto_sessions(date(2024, 1, 5), date(2024, 1, 7)).expect("crypto");
    assert_eq!(sessions.len(), 3);
    assert_eq!(
        sessions[0].open_time.isoformat_seconds_z(),
        "2024-01-05T00:00:00Z"
    );
    assert_eq!(
        sessions[0].close_time.isoformat_seconds_z(),
        "2024-01-06T00:00:00Z"
    );
    assert_eq!(sessions[2].session_date.isoformat(), "2024-01-07");
}

#[test]
fn is_partial_uses_the_half_open_session_window() {
    let session = xnys_sessions(date(2024, 1, 18), date(2024, 1, 18))
        .expect("session")
        .remove(0);
    assert!(is_partial(&session, session.open_time));
    assert!(is_partial(
        &session,
        DateTime::from_micros(session.close_time.micros() - 1)
    ));
    assert!(!is_partial(&session, session.close_time));
    assert!(!is_partial(
        &session,
        DateTime::from_micros(session.open_time.micros() - 1)
    ));
}

#[test]
fn sessions_for_rejects_unsupported_asset_classes() {
    assert!(sessions_for(AssetClass::Equity, date(2024, 1, 18), date(2024, 1, 19)).is_ok());
    assert!(sessions_for(AssetClass::Crypto, date(2024, 1, 18), date(2024, 1, 19)).is_ok());
    assert!(sessions_for(AssetClass::Option, date(2024, 1, 18), date(2024, 1, 19)).is_err());
}

#[test]
fn inverted_ranges_are_rejected() {
    assert!(xnys_sessions(date(2024, 1, 19), date(2024, 1, 18)).is_err());
    assert!(crypto_utc_days(date(2024, 1, 19), date(2024, 1, 18)).is_err());
}
