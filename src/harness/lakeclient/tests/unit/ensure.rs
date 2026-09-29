use super::*;

fn entry(status: &str) -> CoverageEntry {
    CoverageEntry {
        session_date: "2024-01-02".to_string(),
        status: status.to_string(),
        is_partial: false,
        symbol: None,
        data_type: None,
        error_message: None,
    }
}

fn open_session() -> CoverageEntry {
    CoverageEntry {
        is_partial: true,
        session_date: "2024-01-02".to_string(),
        status: "partial".to_string(),
        symbol: Some("SPY".to_string()),
        data_type: Some("trade".to_string()),
        error_message: None,
    }
}

fn closed_partial() -> CoverageEntry {
    CoverageEntry {
        session_date: "2026-09-15".to_string(),
        status: "partial".to_string(),
        symbol: Some("SPY".to_string()),
        data_type: Some("trade".to_string()),
        ..entry("partial")
    }
}

fn error_session() -> CoverageEntry {
    CoverageEntry {
        session_date: "2024-01-02".to_string(),
        status: "error".to_string(),
        symbol: Some("IWM".to_string()),
        data_type: Some("quote".to_string()),
        error_message: Some("provider rejected the window".to_string()),
        ..entry("error")
    }
}

#[test]
fn an_open_session_is_satisfied_even_though_it_is_partial() {
    let entries = vec![
        entry("complete"),
        entry("empty"),
        entry("delisted"),
        open_session(),
    ];
    assert!(coverage_entries_satisfied(&entries));
    assert!(coverage_incompleteness(&entries).is_none());
}

#[test]
fn a_closed_partial_session_is_an_incompleteness_failure() {
    let entries = vec![entry("complete"), closed_partial()];
    assert!(!coverage_entries_satisfied(&entries));
    let message = coverage_incompleteness(&entries).expect("partial must be reported");
    assert!(message.contains("SPY"));
    assert!(message.contains("2026-09-15"));
    assert!(message.contains("partial"));
}

#[test]
fn an_error_session_is_an_incompleteness_failure() {
    let entries = vec![error_session()];
    assert!(!coverage_entries_satisfied(&entries));
    let message = coverage_incompleteness(&entries).expect("an error session must be reported");
    assert!(message.contains("IWM"));
    assert!(message.contains("provider rejected the window"));
}

#[test]
fn missing_sessions_are_polled_rather_than_failed() {
    let entries = vec![entry("complete"), entry("missing")];
    assert!(!coverage_entries_satisfied(&entries));
    assert!(coverage_incompleteness(&entries).is_none());
    assert!(!coverage_entries_satisfied(&[]));
}

#[test]
fn the_poll_cadence_backs_off_to_a_fixed_maximum() {
    let mut interval = Duration::from_millis(1_000);
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
    let entries = vec![
        entry("empty"),
        CoverageEntry {
            session_date: "2005-01-03".to_string(),
            ..entry("empty")
        },
        CoverageEntry {
            session_date: "2015-12-09".to_string(),
            ..entry("missing")
        },
        CoverageEntry {
            session_date: "2015-12-10".to_string(),
            ..entry("complete")
        },
        CoverageEntry {
            session_date: "2015-12-11".to_string(),
            ..entry("complete")
        },
        CoverageEntry {
            session_date: "2016-01-04".to_string(),
            ..entry("error")
        },
        CoverageEntry {
            session_date: "2020-03-13".to_string(),
            ..entry("delisted")
        },
    ];
    assert_eq!(
        earliest_complete_date(&entries),
        Some("2015-12-10".to_string())
    );
}

#[test]
fn a_window_without_complete_sessions_has_no_earliest_session() {
    let entries = vec![entry("empty"), entry("missing")];
    assert_eq!(earliest_complete_date(&entries), None);
    assert_eq!(earliest_complete_date(&[]), None);
}

#[test]
fn ensure_job_progress_classifies_in_flight_settled_and_failed() {
    let running = JobStatus {
        job_id: "job-1".to_string(),
        state: "running".to_string(),
        error_message: None,
    };
    assert_eq!(ensure_job_progress(&running), EnsureJobProgress::InFlight);
    let queued = JobStatus {
        state: "queued".to_string(),
        ..running.clone()
    };
    assert_eq!(ensure_job_progress(&queued), EnsureJobProgress::InFlight);
    let complete = JobStatus {
        state: "complete".to_string(),
        ..running.clone()
    };
    assert_eq!(ensure_job_progress(&complete), EnsureJobProgress::Settled);
    let failed = JobStatus {
        state: "failed".to_string(),
        error_message: Some("boom".to_string()),
        ..running.clone()
    };
    assert_eq!(
        ensure_job_progress(&failed),
        EnsureJobProgress::Failed("boom".to_string())
    );
    let failed_without_message = JobStatus {
        state: "failed".to_string(),
        ..running
    };
    assert_eq!(
        ensure_job_progress(&failed_without_message),
        EnsureJobProgress::Failed("the ensure job failed".to_string())
    );
}

#[test]
fn a_closed_partial_is_repaired_rather_than_failed_while_the_job_runs() {
    // The lake classifies a closed-session partial as an actionable gap and
    // re-fetches it inside the running job, so the ensure must keep polling
    // instead of aborting the very repair that would clear it.
    assert_eq!(
        coverage_verdict(&[closed_partial()], &EnsureJobProgress::InFlight),
        CoverageVerdict::Repairing
    );
}

#[test]
fn a_closed_partial_that_survives_the_job_is_a_failure() {
    match coverage_verdict(&[closed_partial()], &EnsureJobProgress::Settled) {
        CoverageVerdict::Incomplete(message) => {
            assert!(message.contains("2026-09-15"), "{message}");
            assert!(message.contains("partial"), "{message}");
        }
        other => panic!("expected a settled failure, got {other:?}"),
    }
}

#[test]
fn an_errored_fetch_is_repaired_rather_than_failed_while_the_job_runs() {
    let entries = [error_session()];
    assert_eq!(
        coverage_verdict(&entries, &EnsureJobProgress::InFlight),
        CoverageVerdict::Repairing
    );
    assert!(matches!(
        coverage_verdict(&entries, &EnsureJobProgress::Settled),
        CoverageVerdict::Incomplete(_)
    ));
}

#[test]
fn a_satisfied_window_wins_even_from_a_settled_job() {
    let entries = [entry("complete"), entry("empty")];
    assert_eq!(
        coverage_verdict(&entries, &EnsureJobProgress::Settled),
        CoverageVerdict::Satisfied
    );
}

#[test]
fn a_failed_job_reports_its_own_error() {
    match coverage_verdict(
        &[entry("missing")],
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
