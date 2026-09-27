use super::*;

#[test]
fn stable_dead_letter_id_is_deterministic_and_input_sensitive() {
    let first = stable_dead_letter_id("equity-backfill", "message-1", r#"{"job_id":"job-1"}"#);
    let again = stable_dead_letter_id("equity-backfill", "message-1", r#"{"job_id":"job-1"}"#);
    let other_content =
        stable_dead_letter_id("equity-backfill", "message-1", r#"{"job_id":"job-2"}"#);
    let other_queue =
        stable_dead_letter_id("options-backfill", "message-1", r#"{"job_id":"job-1"}"#);

    assert_eq!(first, again);
    assert_eq!(first.len(), 64);
    assert_ne!(first, other_content);
    assert_ne!(first, other_queue);

    // The identity JSON matches Python's compact, key-sorted, ensure-ASCII
    // encoding exactly for representative ASCII inputs.
    let expected = hex::encode(sha2::Sha256::digest(
        br#"{"content":"{\"job_id\":\"job-1\"}","message_id":"message-1","source_queue":"equity-backfill"}"#,
    ));
    assert_eq!(first, expected);
}

#[test]
fn json_ensure_ascii_string_matches_python_json_dumps() {
    assert_eq!(json_ensure_ascii_string("plain"), r#""plain""#);
    assert_eq!(json_ensure_ascii_string("a\"b\\c"), r#""a\"b\\c""#);
    assert_eq!(
        json_ensure_ascii_string("tab\tnewline\n"),
        "\"tab\\tnewline\\n\""
    );
    assert_eq!(
        json_ensure_ascii_string("\u{1}\u{1f}"),
        "\"\\u0001\\u001f\""
    );
    assert_eq!(json_ensure_ascii_string("\u{7f}"), "\"\\u007f\"");
    assert_eq!(json_ensure_ascii_string("\u{e9}"), "\"\\u00e9\"");
    assert_eq!(json_ensure_ascii_string("\u{1F600}"), "\"\\ud83d\\ude00\"");
}

#[test]
fn isoformat_utc_matches_python_isoformat() {
    let instant = DateTime::from_ymd_hms(2026, 9, 1, 23, 0, 0, 0).expect("instant");
    assert_eq!(isoformat_utc(instant), "2026-09-01T23:00:00+00:00");
    let micros = DateTime::from_ymd_hms(2026, 9, 1, 23, 0, 0, 500).expect("instant");
    assert_eq!(isoformat_utc(micros), "2026-09-01T23:00:00.000500+00:00");
}

#[test]
fn identifier_check_matches_python_isidentifier() {
    assert!(is_python_identifier("ValueError"));
    assert!(is_python_identifier("_Private"));
    assert!(!is_python_identifier("Not A Name"));
    assert!(!is_python_identifier("3rd"));
    assert!(!is_python_identifier(""));
}

#[test]
fn offset_detection_requires_timezone_aware_instants() {
    assert!(has_explicit_offset("2026-09-01T23:00:00+00:00"));
    assert!(has_explicit_offset("2026-09-01T23:00:00Z"));
    assert!(has_explicit_offset("2026-09-01T17:00:00-06:00"));
    assert!(!has_explicit_offset("2026-09-01T23:00:00"));
}
