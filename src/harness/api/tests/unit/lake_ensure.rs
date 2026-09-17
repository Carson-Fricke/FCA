use super::*;
use serde_json::json;

#[test]
fn terminal_statuses_cover_success_empty_and_delisted() {
    assert!(is_terminal_status("complete"));
    assert!(is_terminal_status("empty"));
    assert!(is_terminal_status("delisted"));
    assert!(!is_terminal_status("missing"));
    assert!(!is_terminal_status("partial"));
    assert!(!is_terminal_status("error"));
}

#[test]
fn coverage_is_terminal_only_when_every_entry_is_terminal() {
    let covered = json!({
        "entries": [
            {"status": "complete"},
            {"status": "empty"},
        ]
    });
    assert!(coverage_is_terminal(&covered));

    let pending = json!({
        "entries": [
            {"status": "complete"},
            {"status": "missing"},
        ]
    });
    assert!(!coverage_is_terminal(&pending));

    assert!(!coverage_is_terminal(&json!({"job_id": "ensure-1"})));
    assert!(!coverage_is_terminal(&json!({"entries": []})));
}
