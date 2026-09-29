use super::*;
use crate::time::Date;

/// The harness facade must resolve sessions through the contract calendar,
/// including the dates the old harness-local copy got wrong.
#[test]
fn xnys_facade_honors_contract_calendar() {
    // 2018-12-05 is a contract-pinned ad-hoc closure (George H. W. Bush
    // funeral).
    let closure = Date::from_ymd(2018, 12, 5).unwrap();
    assert!(xnys_sessions(closure, closure).unwrap().is_empty());

    // 2024-12-24 is a contract-pinned early close (13:00 ET = 18:00 UTC).
    let christmas_eve = Date::from_ymd(2024, 12, 24).unwrap();
    let sessions = xnys_sessions(christmas_eve, christmas_eve).unwrap();
    assert_eq!(sessions.len(), 1);
    assert_eq!(
        sessions[0].close_time.isoformat_seconds_z(),
        "2024-12-24T18:00:00Z"
    );
}
