use super::*;
use crate::http::clock::SystemClock;
use crate::http::store::{InMemoryJobRepository, InMemoryRunStore};
use std::sync::Arc;

struct StubRunner;

impl BacktestRunner for StubRunner {
    fn run(&self, _configuration: &ValidatedConfiguration) -> Result<RunOutcome, String> {
        let mut artifacts = BTreeMap::new();
        artifacts.insert(
            "backtest.json".to_string(),
            b"{\"status\":\"completed\"}".to_vec(),
        );
        Ok(RunOutcome {
            completed: true,
            artifacts,
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
        screen: None,
    }
}

#[test]
fn launch_record_joins_sorted_strategy_ids() {
    let record = launch_record(
        &config(),
        &RunOutcome {
            completed: true,
            artifacts: BTreeMap::new(),
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

#[test]
fn artifact_metrics_flattens_custom_numeric_observations() {
    let mut artifacts = BTreeMap::new();
    artifacts.insert(
        "metrics.json".to_string(),
        br#"{"equity":{"total_return":"0.1"},"execution":{"fill_count":2},"custom":{"rolling_corr_60d_median":"0.73"}}"#.to_vec(),
    );

    let metrics = artifact_metrics(&artifacts);

    assert_eq!(metrics.get("total_return").map(String::as_str), Some("0.1"));
    assert_eq!(metrics.get("fill_count").map(String::as_str), Some("2"));
    assert_eq!(
        metrics.get("rolling_corr_60d_median").map(String::as_str),
        Some("0.73")
    );
}
