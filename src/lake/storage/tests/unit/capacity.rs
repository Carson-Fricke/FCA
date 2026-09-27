use super::*;
use serde_json::json;
use std::sync::Arc;

struct FixedUsage(u64);

impl StorageUsageProvider for FixedUsage {
    fn current_storage(&self) -> Result<u64, DataError> {
        Ok(self.0)
    }
}

struct FailingUsage;

impl StorageUsageProvider for FailingUsage {
    fn current_storage(&self) -> Result<u64, DataError> {
        Err(DataError::io("storage usage unavailable"))
    }
}

fn policy(capacity: u64, pause: bool) -> StorageCapPolicy {
    StorageCapPolicy {
        capacity_bytes: capacity,
        pause_enqueue_at_capacity: pause,
        pause_ingest_at_capacity: pause,
    }
}

fn guard(capacity: u64, current: u64, pause: bool) -> StorageCapGuard {
    StorageCapGuard::new(policy(capacity, pause), Arc::new(FixedUsage(current)))
}

#[test]
fn decisions_cover_every_operation_pause_and_projection_combination() {
    let cases = [
        (50u64, 0u64, 50u64, StorageCapState::Available, None),
        (99, 0, 99, StorageCapState::Available, None),
        (
            99,
            1,
            100,
            StorageCapState::AtCapacity,
            Some("capacity_reached"),
        ),
        (
            100,
            0,
            100,
            StorageCapState::AtCapacity,
            Some("capacity_reached"),
        ),
        (
            101,
            0,
            101,
            StorageCapState::OverCapacity,
            Some("capacity_would_be_exceeded"),
        ),
        (
            99,
            2,
            101,
            StorageCapState::OverCapacity,
            Some("capacity_would_be_exceeded"),
        ),
    ];
    for operation in [StorageOperation::Enqueue, StorageOperation::Ingest] {
        for pause in [true, false] {
            for (current, additional, projected, state, reason) in cases {
                let guard = guard(100, current, pause);
                let status = guard.evaluate(operation, additional).expect("status");
                let expected_state = if pause && projected >= 100 {
                    StorageCapState::Paused
                } else {
                    state
                };
                assert_eq!(
                    status.operation, operation,
                    "operation {operation:?} pause={pause} current={current} additional={additional}"
                );
                assert_eq!(
                    status.state, expected_state,
                    "state for {operation:?} pause={pause} current={current} additional={additional}"
                );
                assert_eq!(status.current_bytes, current);
                assert_eq!(status.projected_bytes, projected);
                assert_eq!(status.capacity_bytes, 100);
                assert_eq!(status.pause_enabled, pause);
                assert_eq!(
                    status.reason.as_deref(),
                    reason,
                    "reason for {operation:?} pause={pause} current={current} additional={additional}"
                );
                assert_eq!(guard.last_status(), Some(status.clone()));
            }
        }
    }
}

#[test]
fn alert_shape_is_stable_for_paused_at_capacity_status() {
    let guard = guard(100, 100, true);
    let status = guard
        .evaluate(StorageOperation::Enqueue, 0)
        .expect("status");

    assert!(status.paused());
    assert!(status.alertable());
    assert_eq!(status.remaining_bytes(), 0);
    assert_eq!(
        status.as_alert(),
        json!({
            "alertable": true,
            "capacity_bytes": 100,
            "current_bytes": 100,
            "operation": "enqueue",
            "pause_enabled": true,
            "projected_bytes": 100,
            "reason": "capacity_reached",
            "state": "paused",
        })
    );
}

#[test]
fn alert_shape_omits_a_reason_when_available() {
    let guard = guard(100, 10, true);
    let status = guard.evaluate(StorageOperation::Ingest, 5).expect("status");

    assert!(!status.paused());
    assert!(!status.alertable());
    assert_eq!(status.remaining_bytes(), 85);
    assert_eq!(status.as_alert()["reason"], Value::Null);
    assert_eq!(status.as_alert()["operation"], "ingest");
    assert_eq!(status.as_alert()["state"], "available");
}

#[test]
fn enforce_enqueue_conflicts_only_when_paused() {
    let paused = guard(100, 100, true);
    let error = paused.enforce_enqueue().expect_err("paused");
    match error {
        DataError::Conflict(message) => {
            assert!(message.contains("paused enqueue"), "{message}");
            assert!(message.contains("100 of 100 bytes"), "{message}");
            assert!(message.contains("capacity_reached"), "{message}");
        }
        other => panic!("expected conflict, got {other:?}"),
    }
    assert_eq!(
        paused.last_status().expect("status").state,
        StorageCapState::Paused
    );

    let allowed = guard(100, 100, false);
    let status = allowed.enforce_enqueue().expect("allowed");
    assert_eq!(status.state, StorageCapState::AtCapacity);
    assert!(status.alertable());
    assert!(!status.paused());
}

#[test]
fn enforce_ingest_accounts_for_staged_bytes() {
    let paused_ingest = guard(100, 90, true);
    let error = paused_ingest.enforce_ingest(10).expect_err("paused");
    assert!(matches!(error, DataError::Conflict(_)), "{error:?}");
    assert_eq!(
        paused_ingest.last_status().expect("status").projected_bytes,
        100
    );

    let status = paused_ingest.enforce_ingest(9).expect("allowed");
    assert_eq!(status.state, StorageCapState::Available);
    assert_eq!(status.remaining_bytes(), 1);

    let disabled = guard(100, 90, false);
    let status = disabled.enforce_ingest(10).expect("allowed at capacity");
    assert_eq!(status.state, StorageCapState::AtCapacity);
}

#[test]
fn projected_bytes_never_underflow_remaining() {
    let guard = guard(100, 250, false);
    let status = guard.evaluate(StorageOperation::Ingest, 0).expect("status");
    assert_eq!(status.state, StorageCapState::OverCapacity);
    assert_eq!(status.remaining_bytes(), 0);
    assert!(status.alertable());
}

#[test]
fn sum_byte_sizes_adds_persisted_partition_bytes() {
    assert_eq!(sum_byte_sizes(std::iter::empty()), 0);
    assert_eq!(sum_byte_sizes([120u64, 80].into_iter()), 200);
}

#[test]
fn usage_provider_failures_propagate_without_recording_status() {
    let guard = StorageCapGuard::new(policy(1, true), Arc::new(FailingUsage));
    let error = guard
        .evaluate(StorageOperation::Enqueue, 0)
        .expect_err("provider failure");
    assert!(matches!(error, DataError::Io(_)), "{error:?}");
    assert!(guard.last_status().is_none());
}
