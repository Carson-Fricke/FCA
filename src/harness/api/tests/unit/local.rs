use super::*;
use mcp::store::{FileSystemJobRepository, FileSystemRunStore};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
}

/// Build one small real SPY lake shared by every submitting test, and point
/// `HARNESS_DATA_DIR` at it: the submitters replay actual lake data, never a
/// substitute fixture. Each session carries a rising quote ladder (so the
/// momentum strategy enters and liquidates) plus one trade (so research
/// models observe trades).
fn use_shared_lake() {
    static LAKE: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();
    let lake = LAKE.get_or_init(|| {
        use backtest::backtest::errors::ONE_DAY_NS;
        use marketdata::events::{MarketEvent as LakeEvent, QuoteEvent, TradeEvent};
        use marketdata::time::DateTime;
        use marketdata::{write_lake_partition, AssetClass, DataType, Date};

        let lake =
            std::env::temp_dir().join(format!("harness-api-local-lake-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&lake);
        for day_offset in 0..3 {
            let date = Date::from_ymd(2024, 1, 2 + day_offset).expect("date");
            let day = i64::from(date.days());
            let base = day * ONE_DAY_NS + (13 * 3_600 + 30 * 60) * 1_000_000_000;
            let mut quotes = Vec::new();
            for step in 0..12i64 {
                let mid = 100.00 + 0.02 * step as f64;
                quotes.push(LakeEvent::Quote(QuoteEvent {
                    event_id: format!("lake-quote-{day_offset}-{step}"),
                    timestamp: DateTime::from_micros(
                        (base + (32 * 60 * step) * 1_000_000_000) / 1_000,
                    ),
                    sequence: step as u64,
                    symbol: "SPY".to_string(),
                    bid_price: format!("{:.2}", mid - 0.01).parse().unwrap(),
                    ask_price: format!("{:.2}", mid + 0.01).parse().unwrap(),
                    bid_size: "100".parse().unwrap(),
                    ask_size: "100".parse().unwrap(),
                    venue: Some("XNYS".to_string()),
                }));
            }
            let trade = LakeEvent::Trade(TradeEvent {
                event_id: format!("lake-trade-{day_offset}"),
                timestamp: DateTime::from_micros((base + 60 * 1_000_000_000) / 1_000),
                sequence: 12,
                symbol: "SPY".to_string(),
                price: "100.02".parse().unwrap(),
                quantity: "20".parse().unwrap(),
                venue: Some("XNYS".to_string()),
                trade_id: Some(format!("lake-trade-id-{day_offset}")),
            });
            write_lake_partition(
                &lake,
                date,
                AssetClass::Equity,
                DataType::Quote,
                "SPY",
                &quotes,
            )
            .expect("write quote lake");
            write_lake_partition(
                &lake,
                date,
                AssetClass::Equity,
                DataType::Trade,
                "SPY",
                std::slice::from_ref(&trade),
            )
            .expect("write trade lake");
        }
        lake
    });
    std::env::set_var("HARNESS_DATA_DIR", lake);
}

fn poll_terminal(jobs: &Arc<dyn JobRepository>, job_id: &str) -> JobState {
    for _ in 0..600 {
        if let Some(record) = jobs.get(job_id) {
            if matches!(record.state, JobState::Succeeded | JobState::Failed) {
                if record.state == JobState::Failed {
                    eprintln!("job {job_id} failed: {:?}", record.error_message);
                }
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
    use_shared_lake();
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
fn native_submitter_persists_authored_study_custom_metrics() {
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
    use_shared_lake();
    let study_source = r#"
use harness_strategy_sdk::{
    export_study_program, MetricPublisher, QuoteEvent, StudyProgram, TradeEvent,
};
use harness_strategy_sdk::serde_json::Value;

/// Count the observed events and publish the mean trade price.
struct TradeObserver {
    trades: u64,
    quotes: u64,
    price_sum: f64,
}

impl StudyProgram for TradeObserver {
    fn new(_parameters: Value) -> Self {
        Self { trades: 0, quotes: 0, price_sum: 0.0 }
    }

    fn on_quote(&mut self, _event: &QuoteEvent) {
        self.quotes += 1;
    }

    fn on_trade(&mut self, event: &TradeEvent) {
        self.trades += 1;
        if let Ok(price) = event.price.to_string().parse::<f64>() {
            self.price_sum += price;
        }
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("trade_observations", self.trades as f64);
        let _ = metrics.publish("quote_observations", self.quotes as f64);
        if self.trades > 0 {
            let _ = metrics.publish("global_sma", self.price_sum / self.trades as f64);
        }
    }
}

export_study_program!(TradeObserver);
"#;
    let digest = backtest::sha256::digest_hex(study_source.as_bytes());
    let document = json!({
        "schema_version": 1,
        "shell": "study",
        "run_id": "workspace-research-metric",
        "seed": 1729,
        "data": {"symbols": ["SPY"]},
        "strategy_instances": [{
            "instance_id": "research-model",
            "implementation": "jit",
            "bundle_hash": digest,
            "capital_fraction": "1.0",
            "symbols": ["SPY"],
            "parameters": {
                "source": study_source
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

#[test]
fn session_dates_round_trip_through_nanos() {
    let nanos = date_ns("2015-12-10").expect("session date");
    assert_eq!(date_string(nanos), "2015-12-10");
    assert_eq!(
        date_of(nanos),
        chrono::NaiveDate::from_ymd_opt(2015, 12, 10)
    );
    assert!(date_ns("not-a-date").is_none());
}

#[test]
fn the_window_start_clamps_forward_to_the_first_complete_session() {
    let requested = date_ns("2005-01-03").expect("requested start");
    let end = date_ns("2026-01-01").expect("end");
    let earliest = date_ns("2015-12-10").expect("earliest");
    assert_eq!(
        clamped_start_ns(requested, end, Some(earliest)),
        Some(earliest)
    );
}

#[test]
fn a_window_start_never_clamps_backward_or_into_an_empty_window() {
    let requested = date_ns("2016-01-04").expect("requested start");
    let end = date_ns("2026-01-01").expect("end");
    let earliest = date_ns("2015-12-10").expect("earliest");
    assert_eq!(clamped_start_ns(requested, end, Some(earliest)), None);
    assert_eq!(clamped_start_ns(requested, end, Some(requested)), None);
    assert_eq!(clamped_start_ns(requested, end, Some(end)), None);
    assert_eq!(clamped_start_ns(requested, end, None), None);
}

#[test]
fn background_job_guard_fails_a_job_left_in_flight() {
    let root = std::env::temp_dir().join(format!(
        "harness-job-guard-{}-{}",
        std::process::id(),
        Utc::now().timestamp_nanos_opt().unwrap_or(0)
    ));
    let _ = fs::remove_dir_all(&root);
    let jobs: Arc<dyn JobRepository> = Arc::new(FileSystemJobRepository::new(&root));
    let started = Utc::now();
    jobs.upsert(mcp::store::JobRecord::new(
        "guard-job",
        JobState::Running,
        started,
        started,
    ));

    drop(BackgroundJobGuard::new(jobs.clone(), "guard-job", started));

    let record = jobs.get("guard-job").expect("record");
    assert_eq!(record.state, JobState::Failed);
    assert!(record.error_message.is_some());
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn background_job_guard_leaves_a_terminal_job_alone() {
    let root = std::env::temp_dir().join(format!(
        "harness-job-guard-terminal-{}-{}",
        std::process::id(),
        Utc::now().timestamp_nanos_opt().unwrap_or(0)
    ));
    let _ = fs::remove_dir_all(&root);
    let jobs: Arc<dyn JobRepository> = Arc::new(FileSystemJobRepository::new(&root));
    let started = Utc::now();
    jobs.upsert(mcp::store::JobRecord::new(
        "guard-job",
        JobState::Succeeded,
        started,
        started,
    ));

    drop(BackgroundJobGuard::new(jobs.clone(), "guard-job", started));

    let record = jobs.get("guard-job").expect("record");
    assert_eq!(record.state, JobState::Succeeded);
    assert!(record.error_message.is_none());
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn panic_payloads_render_as_messages() {
    let static_message: Box<dyn std::any::Any + Send> = Box::new("static boom");
    assert_eq!(panic_message(static_message.as_ref()), "static boom");
    let owned: Box<dyn std::any::Any + Send> = Box::new("owned boom".to_string());
    assert_eq!(panic_message(owned.as_ref()), "owned boom");
    let opaque: Box<dyn std::any::Any + Send> = Box::new(7_u32);
    assert_eq!(panic_message(opaque.as_ref()), "unknown panic");
}
