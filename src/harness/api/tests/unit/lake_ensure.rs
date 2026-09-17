use super::*;
use serde_json::json;

#[test]
fn an_open_session_is_satisfied_even_though_it_is_partial() {
    let entries = json!({
        "entries": [
            {"status": "complete"},
            {"status": "empty"},
            {"status": "delisted"},
            {"status": "partial", "is_partial": true},
        ]
    });
    assert!(coverage_is_satisfied(&entries));
    assert!(coverage_incompleteness(&entries).is_none());
}

#[test]
fn a_closed_partial_session_is_an_incompleteness_failure() {
    let entries = json!({
        "entries": [
            {"status": "complete"},
            {
                "status": "partial",
                "is_partial": false,
                "symbol": "IWM",
                "data_type": "trade",
                "session_date": "2024-01-02",
            },
        ]
    });
    assert!(!coverage_is_satisfied(&entries));
    let message = coverage_incompleteness(&entries).expect("partial must be reported");
    assert!(message.contains("IWM"));
    assert!(message.contains("2024-01-02"));
    assert!(message.contains("partial"));
}

#[test]
fn an_error_session_is_an_incompleteness_failure() {
    let entries = json!({
        "entries": [
            {
                "status": "error",
                "is_partial": false,
                "symbol": "IWM",
                "data_type": "quote",
                "session_date": "2024-01-02",
                "error_message": "provider rejected the window",
            },
        ]
    });
    assert!(!coverage_is_satisfied(&entries));
    let message = coverage_incompleteness(&entries).expect("an error session must be reported");
    assert!(message.contains("IWM"));
    assert!(message.contains("provider rejected the window"));
}

#[test]
fn missing_sessions_are_polled_rather_than_failed() {
    let entries = json!({
        "entries": [
            {"status": "complete"},
            {"status": "missing", "is_partial": false},
        ]
    });
    assert!(!coverage_is_satisfied(&entries));
    assert!(coverage_incompleteness(&entries).is_none());
    assert!(!coverage_is_satisfied(&json!({"job_id": "ensure-1"})));
    assert!(!coverage_is_satisfied(&json!({"entries": []})));
}

#[test]
fn ensure_timeout_allows_a_full_lake_backfill() {
    assert!(DEFAULT_ENSURE_TIMEOUT_SECONDS >= 3_600);
    assert!(MAX_ENSURE_RANGE_DAYS > 0);
}
