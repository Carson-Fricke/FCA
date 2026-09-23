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
fn the_poll_cadence_backs_off_to_a_fixed_maximum() {
    let mut interval = std::time::Duration::from_millis(1_000);
    let steps = [
        Duration::from_millis(2_000),
        Duration::from_millis(4_000),
        Duration::from_millis(5_000),
        Duration::from_millis(5_000),
    ];
    for expected in steps {
        interval = next_poll_interval(interval);
        assert_eq!(interval, expected);
    }
}

#[test]
fn the_earliest_complete_session_is_the_minimum_complete_date() {
    let body = json!({
        "entries": [
            {"status": "empty", "session_date": "2005-01-03"},
            {"status": "missing", "session_date": "2015-12-09"},
            {"status": "complete", "session_date": "2015-12-10"},
            {"status": "complete", "session_date": "2015-12-11"},
            {"status": "error", "session_date": "2016-01-04"},
            {"status": "delisted", "session_date": "2020-03-13"},
        ]
    });
    assert_eq!(
        earliest_complete_date(&body),
        Some("2015-12-10".to_string())
    );
}

#[test]
fn a_window_without_complete_sessions_has_no_earliest_session() {
    let body = json!({
        "entries": [
            {"status": "empty", "session_date": "2005-01-03"},
            {"status": "missing", "session_date": "2015-12-09"},
        ]
    });
    assert_eq!(earliest_complete_date(&body), None);
    assert_eq!(earliest_complete_date(&json!({"entries": []})), None);
    assert_eq!(earliest_complete_date(&json!({})), None);
}

#[test]
fn ensure_job_progress_classifies_in_flight_settled_and_failed() {
    assert_eq!(
        ensure_job_progress(&json!({"state": "running"})),
        EnsureJobProgress::InFlight
    );
    assert_eq!(
        ensure_job_progress(&json!({"state": "queued"})),
        EnsureJobProgress::InFlight
    );
    assert_eq!(
        ensure_job_progress(&json!({"state": "complete"})),
        EnsureJobProgress::Settled
    );
    assert_eq!(
        ensure_job_progress(&json!({"state": "failed", "error_message": "boom"})),
        EnsureJobProgress::Failed("boom".to_string())
    );
    assert_eq!(
        ensure_job_progress(&json!({"state": "failed"})),
        EnsureJobProgress::Failed("the ensure job failed".to_string())
    );
}

fn closed_partial_coverage() -> serde_json::Value {
    json!({
        "entries": [
            {"status": "complete", "is_partial": false, "session_date": "2024-01-02"},
            {
                "status": "partial",
                "is_partial": false,
                "symbol": "SPY",
                "data_type": "trade",
                "session_date": "2026-09-15",
            },
        ]
    })
}

#[test]
fn a_closed_partial_is_repaired_rather_than_failed_while_the_job_runs() {
    // The lake classifies a closed-session partial as an actionable gap and
    // re-fetches it inside the running job, so the ensure must keep polling
    // instead of aborting the very repair that would clear it.
    assert_eq!(
        coverage_verdict(&closed_partial_coverage(), &EnsureJobProgress::InFlight),
        CoverageVerdict::Repairing
    );
}

#[test]
fn a_closed_partial_that_survives_the_job_is_a_failure() {
    match coverage_verdict(&closed_partial_coverage(), &EnsureJobProgress::Settled) {
        CoverageVerdict::Incomplete(message) => {
            assert!(message.contains("2026-09-15"), "{message}");
            assert!(message.contains("partial"), "{message}");
        }
        other => panic!("expected a settled failure, got {other:?}"),
    }
}

#[test]
fn an_errored_fetch_is_repaired_rather_than_failed_while_the_job_runs() {
    let coverage = json!({
        "entries": [
            {"status": "error", "is_partial": false, "session_date": "2024-01-02"},
        ]
    });
    assert_eq!(
        coverage_verdict(&coverage, &EnsureJobProgress::InFlight),
        CoverageVerdict::Repairing
    );
    assert!(matches!(
        coverage_verdict(&coverage, &EnsureJobProgress::Settled),
        CoverageVerdict::Incomplete(_)
    ));
}

#[test]
fn a_satisfied_window_wins_even_from_a_settled_job() {
    let coverage = json!({"entries": [{"status": "complete"}, {"status": "empty"}]});
    assert_eq!(
        coverage_verdict(&coverage, &EnsureJobProgress::Settled),
        CoverageVerdict::Satisfied
    );
}

#[test]
fn a_failed_job_reports_its_own_error() {
    match coverage_verdict(
        &json!({"entries": [{"status": "missing"}]}),
        &EnsureJobProgress::Failed("provider rejected the window".to_string()),
    ) {
        CoverageVerdict::Incomplete(message) => {
            assert!(
                message.contains("provider rejected the window"),
                "{message}"
            );
        }
        other => panic!("expected a failure, got {other:?}"),
    }
}
