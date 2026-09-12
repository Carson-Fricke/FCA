use super::*;
use mcp::store::{FileSystemJobRepository, FileSystemRunStore};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
}

#[test]
fn canonical_conversion_tags_floats_as_decimals() {
    assert_eq!(to_canonical_json(&json!(4)), JsonValue::Int(4));
    assert_eq!(
        to_canonical_json(&json!("0.05")),
        JsonValue::String("0.05".to_string())
    );
    assert_eq!(
        normalize_parameter(&to_canonical_json(&json!(0.5))),
        JsonValue::String("0.5".to_string())
    );
}

#[test]
fn sweep_spec_parses_the_documented_document() {
    let document = json!({
        "base_config_path": repo_root().join("config/harness/backtest.yaml").to_string_lossy(),
        "instance_id": "spy-momentum",
        "mode": "grid",
        "space": {"lookback": [4, 8]},
    });
    let (spec, _base) = parse_sweep_spec(&document).expect("spec");
    assert_eq!(spec.expand().len(), 2);
}

#[test]
fn sweep_submitter_expands_and_records_a_sweep() {
    let root = std::env::temp_dir().join(format!(
        "harness-sweep-submit-{}-{}",
        std::process::id(),
        Utc::now().timestamp_nanos_opt().unwrap_or(0)
    ));
    let _ = fs::remove_dir_all(&root);
    let run_store: Arc<dyn RunStore> = Arc::new(FileSystemRunStore::new(&root));
    let jobs: Arc<dyn JobRepository> = Arc::new(FileSystemJobRepository::new(&root));
    let submitter = NativeSweepSubmitter::new(run_store, jobs, &root);
    let spec_path = root.join("spec.json");
    fs::create_dir_all(&root).expect("dir");
    let document = json!({
        "base_config_path": repo_root().join("config/harness/backtest.yaml").to_string_lossy(),
        "instance_id": "spy-momentum",
        "mode": "grid",
        "space": {"lookback": [4]},
    });
    fs::write(&spec_path, serde_json::to_vec(&document).expect("json")).expect("write");
    let payload = mcp::tools::SweepSubmitter::submit(&submitter, spec_path.to_str().unwrap())
        .expect("submit");
    assert_eq!(payload["expanded"], 1);
    assert_eq!(payload["run_ids"].as_array().map(Vec::len), Some(1));
    assert!(payload["sweep_id"].as_str().map(str::len) == Some(64));
    let _ = fs::remove_dir_all(&root);
}

fn poll_terminal(jobs: &Arc<dyn JobRepository>, job_id: &str) -> JobState {
    for _ in 0..600 {
        if let Some(record) = jobs.get(job_id) {
            if matches!(record.state, JobState::Succeeded | JobState::Failed) {
                return record.state;
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    panic!("job '{job_id}' did not reach a terminal state");
}

#[test]
fn native_backtest_submitter_is_async_and_registers_the_full_result_set() {
    let root = std::env::temp_dir().join(format!(
        "harness-backtest-submit-{}-{}",
        std::process::id(),
        Utc::now().timestamp_nanos_opt().unwrap_or(0)
    ));
    let _ = fs::remove_dir_all(&root);
    let run_store: Arc<dyn RunStore> = Arc::new(FileSystemRunStore::new(&root));
    let jobs: Arc<dyn JobRepository> = Arc::new(FileSystemJobRepository::new(&root));
    let submitter = NativeBacktestSubmitter::new(run_store.clone(), jobs.clone());
    let config_path = repo_root().join("config/harness/backtest.yaml");
    let output_dir = root.join("output");
    let payload = mcp::tools::BacktestSubmitter::submit(
        &submitter,
        config_path.to_str().expect("config path"),
        Some(output_dir.to_str().expect("output dir")),
    )
    .expect("submit");
    assert_eq!(payload["boundary"], "local");
    assert_eq!(payload["state"], "running");
    assert_eq!(payload["run_id"], "momentum-spy-2024-01");

    let run_id = payload["run_id"].as_str().expect("run id");
    let state = poll_terminal(&jobs, run_id);
    assert_eq!(state, JobState::Succeeded, "job '{run_id}' failed");

    let record = run_store.get(run_id).expect("run row");
    assert_eq!(record.status, RunStatus::Complete);
    assert_eq!(
        record.metrics.get("strategy_id").map(String::as_str),
        Some("spy-momentum")
    );
    assert!(
        record.metrics.contains_key("total_return"),
        "headline metrics were not populated: {:?}",
        record.metrics
    );

    let stored = run_store.fetch(run_id).expect("stored run");
    assert!(stored.artifacts.contains_key("backtest.json"));
    assert!(stored.artifacts.contains_key("metrics.json"));
    assert!(stored.artifacts.contains_key("artifacts/equity.parquet"));

    let _ = fs::remove_dir_all(&root);
}
