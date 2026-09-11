use super::*;
use std::sync::atomic::{AtomicUsize, Ordering};

static COUNTER: AtomicUsize = AtomicUsize::new(0);

fn temp_root(tag: &str) -> PathBuf {
    let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
    let root = std::env::temp_dir().join(format!(
        "harness-store-{}-{tag}-{unique}",
        std::process::id()
    ));
    let _ = fs::remove_dir_all(&root);
    root
}

#[test]
fn run_digest_is_stable_and_domain_separated() {
    let mut digests = BTreeMap::new();
    digests.insert(
        "actions.parquet".to_string(),
        artifact_digest(b"actions-bytes"),
    );
    digests.insert("fills.parquet".to_string(), artifact_digest(b"fills-bytes"));
    let digest = run_digest("run-1", &digests);
    assert_eq!(digest.len(), 64);
    assert_eq!(digest, run_digest("run-1", &digests));
    let mut other = BTreeMap::new();
    other.insert(
        "actions.parquet".to_string(),
        artifact_digest(b"actions-bytes"),
    );
    other.insert("fills.parquet".to_string(), artifact_digest(b"other"));
    assert_ne!(digest, run_digest("run-1", &other));
}

#[test]
fn store_put_derives_digest_and_sizes() {
    let store = InMemoryRunStore::new();
    let mut artifacts = BTreeMap::new();
    artifacts.insert("b.bin".to_string(), vec![1, 2, 3]);
    artifacts.insert("a.bin".to_string(), vec![4, 5]);
    let record = store
        .put(
            RunCatalogRecord::new("r", RunStatus::Complete, "s"),
            artifacts,
        )
        .expect("put");
    assert_eq!(record.artifact_count, 2);
    assert_eq!(record.total_bytes, 5);
    assert_eq!(record.run_digest.as_deref().map(str::len), Some(64));
}

#[test]
fn store_delete_removes_row_and_blobs() {
    let store = InMemoryRunStore::new();
    let mut artifacts = BTreeMap::new();
    artifacts.insert("a.bin".to_string(), vec![1]);
    store
        .put(
            RunCatalogRecord::new("r", RunStatus::Complete, "s"),
            artifacts,
        )
        .expect("put");
    assert!(store.delete("r"));
    assert!(store.get("r").is_none());
    assert!(store.fetch("r").is_none());
    assert!(!store.delete("r"));
}

#[test]
fn store_rejects_unsafe_run_ids() {
    let store = InMemoryRunStore::new();
    let mut artifacts = BTreeMap::new();
    artifacts.insert("a.bin".to_string(), vec![1]);
    let error = store
        .put(
            RunCatalogRecord::new("../x", RunStatus::Complete, "s"),
            artifacts,
        )
        .expect_err("must reject");
    assert!(matches!(error, StoreError::InvalidName(_)));
}

#[test]
fn filesystem_store_reloads_runs_and_artifacts() {
    let root = temp_root("reload");
    let mut artifacts = BTreeMap::new();
    artifacts.insert("backtest.json".to_string(), b"{}".to_vec());
    {
        let store = FileSystemRunStore::new(&root);
        store
            .put(
                RunCatalogRecord::new("run-1", RunStatus::Complete, "momentum"),
                artifacts.clone(),
            )
            .expect("put");
    }
    let reopened = FileSystemRunStore::new(&root);
    let record = reopened.get("run-1").expect("reloaded row");
    assert_eq!(record.strategy_id, "momentum");
    assert_eq!(record.artifact_count, 1);
    let stored = reopened.fetch("run-1").expect("reloaded artifacts");
    assert_eq!(stored.artifacts["backtest.json"], b"{}");
    assert_eq!(reopened.list(None, None).len(), 1);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn filesystem_job_repository_reloads_jobs() {
    let root = temp_root("jobs");
    let now = Utc::now();
    {
        let jobs = FileSystemJobRepository::new(&root);
        jobs.upsert(JobRecord::new("job-1", JobState::Succeeded, now, now));
    }
    let reopened = FileSystemJobRepository::new(&root);
    assert_eq!(
        reopened.get("job-1").map(|job| job.state),
        Some(JobState::Succeeded)
    );
    let _ = fs::remove_dir_all(&root);
}
