use super::*;
use crate::http::clock::SystemClock;
use crate::http::store::{InMemoryJobRepository, InMemoryRunStore};
use std::sync::Arc;

struct StubRunner;

impl BacktestRunner for StubRunner {
    fn run(&self, _configuration: &ValidatedConfiguration) -> Result<RunOutcome, String> {
        Ok(RunOutcome {
            completed: true,
            artifact_json: b"{\"status\":\"completed\"}".to_vec(),
        })
    }
}

fn config() -> ValidatedConfiguration {
    ValidatedConfiguration {
        run_id: "run-1".to_string(),
        strategy_ids: vec!["a".to_string(), "b".to_string()],
        data_start: None,
        data_end: None,
        configuration_digest: "digest".to_string(),
        document: serde_json::Value::Null,
    }
}

#[test]
fn launch_record_joins_sorted_strategy_ids() {
    let record = launch_record(
        &config(),
        &RunOutcome {
            completed: true,
            artifact_json: vec![],
        },
    );
    assert_eq!(record.strategy_id, "a,b");
    assert_eq!(record.status, RunStatus::Complete);
    assert_eq!(record.configuration_digest.as_deref(), Some("digest"));
}

#[test]
fn in_process_launcher_stores_a_run_and_succeeds() {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let launcher = InProcessLauncher::new(
        store.clone(),
        jobs.clone(),
        Arc::new(StubRunner),
        Arc::new(SystemClock),
    );
    launcher
        .launch(&LaunchJob {
            job_id: "run-1".to_string(),
            configuration: config(),
        })
        .expect("launch");
    assert!(store.get("run-1").is_some());
    assert_eq!(
        jobs.get("run-1").map(|job| job.state),
        Some(super::super::store::JobState::Succeeded)
    );
}
