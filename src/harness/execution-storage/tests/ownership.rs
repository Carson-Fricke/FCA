//! Storage-boundary guarantees: claim scope and admitted identity survive both adapters.
use chrono::Utc;
use harness_execution::jobs::{
    ArtifactReader, JobAdmission, JobKind, JobQueries, JobRecord, JobState, ResultPublisher,
};
use harness_execution_storage::{filesystem_store, memory_store};
#[test]
fn every_adapter_preserves_claim_scope_and_admitted_identity() {
    let directory = tempfile::tempdir().unwrap();
    for store in [memory_store(), filesystem_store(directory.path())] {
        let mut record = JobRecord::new("owned", JobKind::Backtest, "input", Utc::now());
        record.accepted_input = serde_json::json!({"job_id":"owned"});
        store.admit(record).unwrap();
        let owned = store.claim("owned").unwrap().unwrap();
        assert!(store.claim("owned").unwrap().is_none());
        assert!(owned.try_get("another").is_err());
        let original = owned.try_get("owned").unwrap().unwrap();
        for mutation in 0..4 {
            let mut next = original.clone();
            next.state = JobState::Failed;
            match mutation {
                0 => next.kind = JobKind::Study,
                1 => next.input_digest = "different".into(),
                2 => next.accepted_input = serde_json::json!({}),
                _ => next.created_at += chrono::Duration::seconds(1),
            }
            assert!(owned.put_result(next, Default::default()).is_err());
            assert_eq!(store.try_get("owned").unwrap().unwrap(), original);
        }
        let mut next = original;
        next.state = JobState::Succeeded;
        owned
            .put_result(next, [("result.json".into(), b"{}".to_vec())].into())
            .unwrap();
        let inventory = store.artifacts("owned").unwrap().unwrap();
        assert_eq!(inventory.len(), 1);
        assert_eq!(inventory[0].size_bytes, 2);
        assert_eq!(
            store.artifact("owned", "result.json").unwrap().unwrap(),
            b"{}"
        );
    }
}
