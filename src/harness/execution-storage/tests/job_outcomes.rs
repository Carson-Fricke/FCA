//! Focused publication-outcome tests for the shared durable job stores.

use std::collections::BTreeMap;
use std::path::PathBuf;

use chrono::Utc;
use harness_execution::jobs::artifact_digest;
use harness_execution::jobs::JobKind;
use harness_execution::jobs::JobRecord;
use harness_execution::jobs::JobState;
use harness_execution::jobs::StoreError;
use harness_execution::jobs::{ArtifactReader, ExecutionRepository, JobAdmission, ResultPublisher};

fn record(job_id: &str, state: JobState) -> JobRecord {
    let mut record = JobRecord::new(
        job_id,
        JobKind::Backtest,
        "input-digest",
        chrono::DateTime::from_timestamp(1_700_000_000, 0).unwrap(),
    );
    record.state = state;
    record
}

fn temp_root(tag: &str) -> PathBuf {
    let unique = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0);
    let root = std::env::temp_dir().join(format!(
        "harness-store-outcomes-{}-{tag}-{unique}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&root);
    root
}

#[test]
fn in_memory_failure_and_cancellation_accept_an_empty_artifact_set() {
    let store = harness_execution_storage::memory_store();
    store.admit(record("job-failed", JobState::Queued)).unwrap();
    let failed = store
        .put_result(record("job-failed", JobState::Failed), BTreeMap::new())
        .expect("failed terminal metadata");
    assert_eq!(failed.state, JobState::Failed);
    assert_eq!(failed.artifact_count, 0);
    assert_eq!(failed.total_bytes, 0);

    store
        .admit(record("job-cancelled", JobState::Queued))
        .unwrap();
    let cancelled = store
        .put_result(
            record("job-cancelled", JobState::Cancelled),
            BTreeMap::new(),
        )
        .expect("cancelled terminal metadata");
    assert_eq!(cancelled.state, JobState::Cancelled);
    assert_eq!(cancelled.artifact_count, 0);
}

#[test]
fn in_memory_success_still_requires_an_artifact() {
    let store = harness_execution_storage::memory_store();
    store
        .admit(record("job-success", JobState::Queued))
        .unwrap();
    assert!(matches!(
        store.put_result(record("job-success", JobState::Succeeded), BTreeMap::new()),
        Err(StoreError::EmptyArtifacts)
    ));
    assert_eq!(
        store.get("job-success").expect("record").state,
        JobState::Queued
    );
}

#[test]
fn in_memory_fetch_distinguishes_empty_from_absent() {
    let store = harness_execution_storage::memory_store();
    store.admit(record("job-empty", JobState::Queued)).unwrap();
    let stored = store.fetch("job-empty").expect("existing job");
    assert!(stored.artifacts.is_empty());
    assert!(store.fetch("job-absent").is_none());
}

#[test]
fn in_memory_terminal_result_cannot_be_overwritten() {
    let store = harness_execution_storage::memory_store();
    store
        .admit(record("job-terminal", JobState::Queued))
        .unwrap();
    store
        .put_result(record("job-terminal", JobState::Failed), BTreeMap::new())
        .expect("failed metadata");
    assert!(matches!(
        store.put_result(record("job-terminal", JobState::Failed), BTreeMap::new()),
        Err(StoreError::Terminal(_))
    ));
    let artifacts = BTreeMap::from([("late.json".to_string(), b"{}".to_vec())]);
    assert!(matches!(
        store.put_result(record("job-terminal", JobState::Succeeded), artifacts),
        Err(StoreError::Terminal(_))
    ));
}

#[test]
fn in_memory_failure_keeps_real_partial_artifacts() {
    let store = harness_execution_storage::memory_store();
    store
        .admit(record("job-partial", JobState::Queued))
        .unwrap();
    let artifacts = BTreeMap::from([("partial.json".to_string(), b"{\"partial\":true}".to_vec())]);
    let failed = store
        .put_result(record("job-partial", JobState::Failed), artifacts)
        .expect("failure with partial output");
    assert_eq!(failed.artifact_count, 1);
    let stored = store.fetch("job-partial").expect("stored");
    assert_eq!(stored.record.state, JobState::Failed);
    assert_eq!(
        stored.artifacts.get("partial.json").map(Vec::as_slice),
        Some(&b"{\"partial\":true}"[..])
    );
}

#[test]
fn filesystem_failure_accepts_an_empty_artifact_set_without_an_objects_directory() {
    let root = temp_root("empty");
    let store = harness_execution_storage::filesystem_store(&root);
    store.admit(record("job-failed", JobState::Queued)).unwrap();
    let failed = store
        .put_result(record("job-failed", JobState::Failed), BTreeMap::new())
        .expect("failed terminal metadata");
    assert_eq!(failed.state, JobState::Failed);
    assert_eq!(failed.artifact_count, 0);
    assert!(!root
        .join("execution")
        .join("objects")
        .join("job-failed")
        .exists());
    let stored = store.fetch("job-failed").expect("existing job");
    assert!(stored.artifacts.is_empty());
    assert!(store.fetch("job-absent").is_none());
    let listed = store.list(None, Some(JobState::Failed), None);
    assert_eq!(
        listed
            .iter()
            .map(|record| record.job_id.as_str())
            .collect::<Vec<_>>(),
        vec!["job-failed"]
    );
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn filesystem_success_still_requires_an_artifact() {
    let root = temp_root("success");
    let store = harness_execution_storage::filesystem_store(&root);
    store
        .admit(record("job-success", JobState::Queued))
        .unwrap();
    assert!(matches!(
        store.put_result(record("job-success", JobState::Succeeded), BTreeMap::new()),
        Err(StoreError::EmptyArtifacts)
    ));
    assert_eq!(
        store.get("job-success").expect("record").state,
        JobState::Queued
    );
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn filesystem_publication_fault_preserves_partial_artifacts() {
    let root = temp_root("fault");
    let store = harness_execution_storage::filesystem_store(&root);
    store.admit(record("job-fault", JobState::Queued)).unwrap();
    let blocked = root
        .join("execution")
        .join("objects")
        .join("job-fault")
        .join(artifact_digest(b"blocked"))
        .join("z-blocked.bin");
    std::fs::create_dir_all(&blocked).unwrap();
    let artifacts = BTreeMap::from([
        ("a-ok.bin".to_string(), b"partial".to_vec()),
        ("z-blocked.bin".to_string(), b"blocked".to_vec()),
    ]);
    let error = store
        .put_result(record("job-fault", JobState::Succeeded), artifacts)
        .expect_err("blocked artifact write must fail");
    assert!(matches!(error, StoreError::Io(_)));
    assert_eq!(
        store.get("job-fault").expect("record").state,
        JobState::Queued
    );
    store
        .put_result(record("job-fault", JobState::Failed), BTreeMap::new())
        .expect("failed terminal metadata");
    let stored = store.fetch("job-fault").expect("stored");
    assert_eq!(stored.record.state, JobState::Failed);
    assert_eq!(
        stored.artifacts.get("a-ok.bin").map(Vec::as_slice),
        Some(&b"partial"[..])
    );
    assert!(!stored.artifacts.contains_key("z-blocked.bin"));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn filesystem_terminal_result_cannot_be_overwritten() {
    let root = temp_root("terminal");
    let store = harness_execution_storage::filesystem_store(&root);
    store
        .admit(record("job-terminal", JobState::Queued))
        .unwrap();
    store
        .put_result(record("job-terminal", JobState::Failed), BTreeMap::new())
        .expect("failed metadata");
    assert!(matches!(
        store.put_result(record("job-terminal", JobState::Cancelled), BTreeMap::new()),
        Err(StoreError::Terminal(_))
    ));
    let artifacts = BTreeMap::from([("late.json".to_string(), b"{}".to_vec())]);
    assert!(matches!(
        store.put_result(record("job-terminal", JobState::Succeeded), artifacts),
        Err(StoreError::Terminal(_))
    ));
    assert!(!root
        .join("execution")
        .join("objects")
        .join("job-terminal")
        .join(artifact_digest(b"{}"))
        .join("late.json")
        .exists());
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn filesystem_content_addressed_inventory_round_trips_object_paths() {
    let root = temp_root("prefix");
    let store = harness_execution_storage::filesystem_store(&root);
    store
        .admit(record("job-artifacts", JobState::Queued))
        .unwrap();
    let artifacts = BTreeMap::from([
        ("metrics.json".to_string(), b"{}".to_vec()),
        (
            "artifacts/equity.parquet".to_string(),
            b"equity-bytes".to_vec(),
        ),
    ]);
    store
        .put_result(record("job-artifacts", JobState::Succeeded), artifacts)
        .expect("successful terminal result");

    let inventory = store.artifacts("job-artifacts").unwrap().unwrap();
    for info in &inventory {
        assert_eq!(
            info.uri,
            format!(
                "execution/objects/job-artifacts/{}/{}",
                info.sha256, info.name
            )
        );
        assert!(root.join(&info.uri).is_file());
    }

    let stored = store.fetch("job-artifacts").expect("stored job");
    assert_eq!(stored.artifacts.len(), 2);
    assert_eq!(
        stored
            .artifacts
            .get("artifacts/equity.parquet")
            .map(Vec::as_slice),
        Some(&b"equity-bytes"[..])
    );
    assert_eq!(stored.record.artifact_count, 2);
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn accepted_input_is_immutable_through_admission_and_result_publication() {
    fn check(store: &dyn ExecutionRepository) {
        let mut original = record("job-input", JobState::Queued);
        original.accepted_input =
            serde_json::json!({"start":"2024-01-01", "parameters":{"lookback":5}});
        store.admit(original.clone()).expect("first admission");
        store.admit(original.clone()).expect("same execution joins");
        let mut altered = original.clone();
        altered.accepted_input["parameters"]["lookback"] = serde_json::json!(6);
        assert!(store.admit(altered.clone()).is_err());
        altered.state = JobState::Failed;
        assert!(store.put_result(altered, BTreeMap::new()).is_err());
        original.state = JobState::Failed;
        store
            .put_result(original.clone(), BTreeMap::new())
            .expect("unchanged input publishes");
        assert_eq!(
            store.get("job-input").unwrap().accepted_input,
            original.accepted_input
        );
    }
    check(&harness_execution_storage::memory_store());
    let root = temp_root("immutable-input");
    let store = harness_execution_storage::filesystem_store(&root);
    check(&store);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn progress_survives_restart_and_terminal_publication_from_an_older_snapshot() {
    use harness_contracts::progress::{ExecutionProgress, ExecutionStage};
    let root = temp_root("progress");
    let store = harness_execution_storage::filesystem_store(&root);
    let accepted = record("job-progress", JobState::Queued);
    store.admit(accepted.clone()).unwrap();
    store.mark_running("job-progress", Utc::now()).unwrap();
    let first = ExecutionProgress::stage(ExecutionStage::ReadingData);
    let initial = store
        .update_progress("job-progress", first.clone(), Utc::now())
        .unwrap();
    let mut next = first;
    next.bytes = 4516000;
    next.rows = 54782;
    store
        .update_progress("job-progress", next, Utc::now())
        .unwrap();
    let reopened = harness_execution_storage::filesystem_store(&root);
    let restored = reopened.get("job-progress").unwrap();
    assert_eq!(restored.accepted_input, accepted.accepted_input);
    assert_eq!(restored.input_digest, accepted.input_digest);
    assert_eq!(restored.progress.len(), 1);
    assert_eq!(
        restored.progress[0].started_at,
        initial.progress[0].started_at
    );
    assert_eq!(restored.progress[0].snapshot.bytes, 4516000);
    let mut failed = initial;
    failed.state = JobState::Failed;
    failed.updated_at = Utc::now();
    failed.error_message = Some("connection reset during quote read".into());
    let failed = reopened.put_result(failed, BTreeMap::new()).unwrap();
    assert_eq!(failed.progress[0].snapshot.bytes, 4516000);
    assert!(failed.progress[0].finished_at.is_some());
    assert!(matches!(
        reopened.update_progress(
            "job-progress",
            ExecutionProgress::stage(ExecutionStage::RunningStudy),
            Utc::now()
        ),
        Err(StoreError::Terminal(_))
    ));
    std::fs::remove_dir_all(root).unwrap();
}

#[path = "support/jobs.rs"]
pub mod job_inspection;
use job_inspection::JobInspection;
