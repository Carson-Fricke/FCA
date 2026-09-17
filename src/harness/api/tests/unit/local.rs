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

#[test]
fn native_submitter_persists_research_model_custom_metrics() {
    let root = std::env::temp_dir().join(format!(
        "harness-research-submit-{}-{}",
        std::process::id(),
        Utc::now().timestamp_nanos_opt().unwrap_or(0)
    ));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).expect("dir");
    let run_store: Arc<dyn RunStore> = Arc::new(FileSystemRunStore::new(&root));
    let jobs: Arc<dyn JobRepository> = Arc::new(FileSystemJobRepository::new(&root));
    let submitter = NativeStudySubmitter::new(run_store.clone(), jobs.clone());
    let config_path = root.join("research.json");
    let model_source = "def fit(context):\n    return {'baseline': 0.5}\n\ndef observe(fit, context):\n    return {'trade_observations': context['indicators']['workspace_trade_count'], 'global_sma': context['indicators']['spy_sma']['value']}\n";
    let indicator_source =
        "def calculate(context, parameters):\n    return len(context['trades'])\n";
    let document = json!({
        "schema_version": 1,
        "shell": "study",
        "run_id": "workspace-research-metric",
        "seed": 1729,
        "data": {"symbols": ["SPY"]},
        "strategy_instances": [{
            "instance_id": "research-model",
            "implementation": "harness.research.study:ResearchModelProgram",
            "bundle_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "capital_fraction": "1.0",
            "symbols": ["SPY"],
            "parameters": {
                "model_source": model_source,
                "model_fit_entrypoint": "fit",
                "model_observe_entrypoint": "observe",
                "indicators": [
                    {
                        "alias": "spy_sma",
                        "scope": "global",
                        "name": "sma",
                        "symbol": "SPY",
                        "parameters": {"period": 2, "bucket": "1d"},
                        "reference": "global:sma"
                    },
                    {
                        "alias": "workspace_trade_count",
                        "scope": "workspace",
                        "name": "trade_count",
                        "symbol": "SPY",
                        "parameters": {},
                        "reference": "workspace:workspace-one/trade_count@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "entrypoint": "calculate",
                        "source": indicator_source
                    }
                ]
            }
        }]
    });
    fs::write(
        &config_path,
        serde_json::to_vec(&document).expect("serialize config"),
    )
    .expect("write config");
    let output_dir = root.join("output");
    let payload = mcp::tools::StudySubmitter::submit(
        &submitter,
        config_path.to_str().expect("config path"),
        Some(output_dir.to_str().expect("output dir")),
    )
    .expect("submit");
    let run_id = payload["study_id"].as_str().expect("study id");
    assert_eq!(poll_terminal(&jobs, run_id), JobState::Succeeded);

    let record = run_store.get(run_id).expect("run row");
    let observed = record
        .metrics
        .get("trade_observations")
        .and_then(|value| value.parse::<f64>().ok())
        .expect("custom metric");
    assert!(observed > 0.0);
    assert!(record
        .metrics
        .get("global_sma")
        .and_then(|value| value.parse::<f64>().ok())
        .is_some());
    let stored = run_store.fetch(run_id).expect("stored run");
    let metrics = stored
        .artifacts
        .get("metrics.json")
        .expect("metrics artifact");
    let document: Value = serde_json::from_slice(metrics).expect("metrics json");
    assert_eq!(
        document["custom"]["trade_observations"]
            .as_str()
            .and_then(|value| value.parse::<f64>().ok()),
        Some(observed)
    );
    assert!(stored.artifacts.contains_key("artifacts/emissions.parquet"));
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn crypto_symbols_map_to_lake_pairs_and_equities_stay_equity() {
    assert_eq!(
        lake_asset_class("BTC-USD"),
        ("crypto", "BTC/USD".to_string())
    );
    assert_eq!(
        lake_asset_class("btc/usd"),
        ("crypto", "BTC/USD".to_string())
    );
    assert_eq!(lake_asset_class("MSTR"), ("equity", "MSTR".to_string()));
}

#[test]
fn date_string_formats_whole_utc_days() {
    // 2024-01-02T00:00:00Z
    assert_eq!(date_string(1_704_153_600_000_000_000), "2024-01-02");
}
