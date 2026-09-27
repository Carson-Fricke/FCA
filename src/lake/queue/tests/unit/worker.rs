use super::*;

#[test]
fn decode_payload_requires_json_objects() {
    let payload = decode_payload(r#"{"job_id":"job-1"}"#).expect("payload");
    assert_eq!(payload, serde_json::json!({"job_id": "job-1"}));

    for content in ["not-json", "[1, 2]", "3", "null", r#""quoted string""#] {
        let error = decode_payload(content).expect_err(content);
        if content == "not-json" {
            assert_eq!(
                error.to_string(),
                "queue message content must be valid JSON"
            );
        } else {
            assert_eq!(
                error.to_string(),
                "queue message content must be a JSON object"
            );
        }
    }
}

#[test]
fn error_message_uses_python_type_names() {
    assert_eq!(
        error_message(&DataError::value("provider unavailable")),
        "ValueError: provider unavailable"
    );
    assert_eq!(
        error_message(&DataError::Io("queue unavailable".into())),
        "IoError: queue unavailable"
    );
    let empty = DataError::value("");
    assert_eq!(error_message(&empty), "ValueError");
}

#[test]
fn message_construction_rejects_invalid_leases() {
    assert_eq!(
        QueueMessage::new("  ", "receipt", "{}", 1)
            .expect_err("message id")
            .to_string(),
        "message_id must not be empty"
    );
    assert_eq!(
        QueueMessage::new("message", "", "{}", 1)
            .expect_err("pop receipt")
            .to_string(),
        "pop_receipt must not be empty"
    );
    assert_eq!(
        QueueMessage::new("message", "receipt", "{}", 0)
            .expect_err("dequeue count")
            .to_string(),
        "dequeue_count must be at least 1"
    );
}

#[test]
fn policy_validates_delivery_controls() {
    assert_eq!(
        QueueWorkerPolicy::new(300, 60, 5, true).expect("policy"),
        QueueWorkerPolicy {
            visibility_timeout_seconds: 300,
            visibility_renewal_seconds: 60,
            max_delivery_attempts: 5,
            dead_letter_on_exhaustion: true,
        }
    );
    for (timeout, renewal, attempts, expected) in [
        (0, 5, 3, "visibility timeout must be positive"),
        (10, 0, 3, "visibility renewal interval must be positive"),
        (
            10,
            10,
            3,
            "visibility renewal interval must be shorter than timeout",
        ),
        (10, 5, 0, "max delivery attempts must be positive"),
    ] {
        let error = QueueWorkerPolicy::new(timeout, renewal, attempts, true).expect_err("policy");
        assert_eq!(error.to_string(), expected);
    }
}

#[test]
fn policy_is_derived_from_t026_settings() {
    let settings = QueueSettings {
        visibility_timeout_seconds: 300,
        visibility_renewal_seconds: 60,
        max_delivery_attempts: 5,
        dead_letter_on_exhaustion: true,
    };
    assert_eq!(
        QueueWorkerPolicy::from_settings(&settings).expect("policy"),
        QueueWorkerPolicy::new(300, 60, 5, true).expect("policy")
    );
}
