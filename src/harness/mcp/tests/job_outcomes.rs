//! Focused publication-outcome tests for the shared durable job stores.

use std::collections::BTreeMap;
use std::path::PathBuf;

use chrono::Utc;
use mcp::store::{
    artifact_prefix, FileSystemJobStore, InMemoryJobStore, JobKind, JobRecord, JobState, JobStore,
    StoreError, ARTIFACT_PREFIX,
};

fn record(job_id: &str, state: JobState) -> JobRecord {
    let mut record = JobRecord::new(job_id, JobKind::Backtest, "input-digest", Utc::now());
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
    let store = InMemoryJobStore::new();
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
    let store = InMemoryJobStore::new();
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
    let store = InMemoryJobStore::new();
    store.admit(record("job-empty", JobState::Queued)).unwrap();
    let stored = store.fetch("job-empty").expect("existing job");
    assert!(stored.artifacts.is_empty());
    assert!(store.fetch("job-absent").is_none());
}

#[test]
fn in_memory_terminal_result_cannot_be_overwritten() {
    let store = InMemoryJobStore::new();
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
    let store = InMemoryJobStore::new();
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
    let store = FileSystemJobStore::new(&root);
    store.admit(record("job-failed", JobState::Queued)).unwrap();
    let failed = store
        .put_result(record("job-failed", JobState::Failed), BTreeMap::new())
        .expect("failed terminal metadata");
    assert_eq!(failed.state, JobState::Failed);
    assert_eq!(failed.artifact_count, 0);
    assert!(!root
        .join("objects")
        .join(ARTIFACT_PREFIX)
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
    let store = FileSystemJobStore::new(&root);
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
    let store = FileSystemJobStore::new(&root);
    store.admit(record("job-fault", JobState::Queued)).unwrap();
    let blocked = root
        .join("objects")
        .join(ARTIFACT_PREFIX)
        .join("job-fault")
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
    let store = FileSystemJobStore::new(&root);
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
        .join("objects")
        .join(ARTIFACT_PREFIX)
        .join("job-terminal")
        .join("late.json")
        .exists());
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn filesystem_artifact_prefix_round_trips_object_paths() {
    let root = temp_root("prefix");
    let store = FileSystemJobStore::new(&root);
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

    assert_eq!(
        artifact_prefix("job-artifacts").expect("prefix"),
        "jobs/job-artifacts"
    );
    assert!(matches!(
        artifact_prefix("bad/id"),
        Err(StoreError::InvalidName(_))
    ));
    assert!(root
        .join("objects")
        .join(ARTIFACT_PREFIX)
        .join("job-artifacts")
        .join("metrics.json")
        .exists());
    assert!(root
        .join("objects")
        .join(ARTIFACT_PREFIX)
        .join("job-artifacts")
        .join("artifacts")
        .join("equity.parquet")
        .exists());
    assert!(!root.join("objects").join("backtests").exists());

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
