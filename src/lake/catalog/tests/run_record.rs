use std::collections::BTreeMap;

use lake_catalog::{
    run_from_entity, run_to_entity, MemoryRunRepository, RunRecord, RunRepository, RunState,
};
use lake_contracts::Date;
use serde_json::{json, Value};

fn sample(job_id: &str, status: RunState) -> RunRecord {
    RunRecord::new(
        job_id,
        status,
        "momentum",
        Some(Date::from_ymd(2024, 1, 2).expect("date")),
        Some(Date::from_ymd(2024, 1, 3).expect("date")),
        Some("digest-1".to_string()),
        Some("config-1".to_string()),
        2,
        24,
        BTreeMap::from([("sharpe".to_string(), "1.2345".to_string())]),
    )
    .expect("record")
}

#[test]
fn run_record_serializes_the_job_key_and_locked_states() {
    let value = serde_json::to_value(sample("job-1", RunState::Succeeded)).expect("serialize");
    assert_eq!(value["job_id"], "job-1");
    assert!(value.get("run_id").is_none());
    assert_eq!(value["status"], "succeeded");
    let states = [
        (RunState::Queued, "queued"),
        (RunState::Running, "running"),
        (RunState::Succeeded, "succeeded"),
        (RunState::Failed, "failed"),
        (RunState::Cancelled, "cancelled"),
    ];
    for (state, text) in states {
        assert_eq!(state.value(), text);
        assert_eq!(RunState::from_value(text), Some(state));
    }
    assert_eq!(RunState::from_value("complete"), None);
}

#[test]
fn run_record_decodes_only_the_job_key() {
    let superseded = json!({
        "run_id": "job-1",
        "status": "succeeded",
        "strategy_id": "momentum",
    });
    assert!(serde_json::from_value::<RunRecord>(superseded).is_err());
    let canonical = json!({
        "job_id": "job-1",
        "status": "cancelled",
        "strategy_id": "momentum",
    });
    let decoded = serde_json::from_value::<RunRecord>(canonical).expect("decode");
    assert_eq!(decoded.job_id, "job-1");
    assert_eq!(decoded.status, RunState::Cancelled);
}

#[test]
fn run_record_memory_repository_keys_on_the_job_identifier() {
    let repository = MemoryRunRepository::new();
    let record = sample("job-1", RunState::Running);
    repository.upsert(&record).expect("upsert");
    assert_eq!(
        repository
            .get(" job-1 ")
            .expect("get")
            .expect("present")
            .job_id,
        "job-1"
    );
    assert_eq!(record.row_key(), "job-1");
    assert!(repository.delete("job-1").expect("delete"));
    assert!(!repository.delete("job-1").expect("delete"));
}

#[test]
fn run_entity_mapping_uses_the_job_column() {
    let record = sample("job-1", RunState::Cancelled);
    let entity = run_to_entity(&record);
    assert_eq!(entity.get("JobId").and_then(Value::as_str), Some("job-1"));
    assert!(entity.get("RunId").is_none());
    assert_eq!(run_from_entity(&entity).expect("restore"), record);
}
