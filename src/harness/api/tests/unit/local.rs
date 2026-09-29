use super::*;
use harness_lakeclient::testing::{
    bars_stream, quote_stream, trade_stream, FakeBarRow, FakeLakeConfig, FakeLakeServer,
    FakeQuoteRow, FakeTradeRow,
};
use mcp::store::{FileSystemJobRepository, FileSystemRunStore};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
}

/// One small real SPY route fixture: each session carries a rising quote
/// ladder (so the momentum strategy enters and liquidates) plus one trade (so
/// research models observe trades). The submitters replay actual lake route
/// events, never a substitute fixture.
fn spy_quote_rows() -> Vec<FakeQuoteRow> {
    use backtest::backtest::errors::ONE_DAY_NS;
    use marketdata::Date;

    let mut rows = Vec::new();
    for day_offset in 0..3 {
        let date = Date::from_ymd(2024, 1, 2 + day_offset).expect("date");
        let base = i64::from(date.days()) * ONE_DAY_NS + (13 * 3_600 + 30 * 60) * 1_000_000_000;
        for step in 0..12i64 {
            let mid = 100.00 + 0.02 * step as f64;
            rows.push(FakeQuoteRow::new(
                base + (32 * 60 * step) * 1_000_000_000,
                &format!("{:.2}", mid - 0.01),
                &format!("{:.2}", mid + 0.01),
                100,
                100,
                "XNYS",
            ));
        }
    }
    rows
}

fn spy_trade_rows() -> Vec<FakeTradeRow> {
    use backtest::backtest::errors::ONE_DAY_NS;
    use marketdata::Date;

    let mut rows = Vec::new();
    for day_offset in 0..3 {
        let date = Date::from_ymd(2024, 1, 2 + day_offset).expect("date");
        let base = i64::from(date.days()) * ONE_DAY_NS + (13 * 3_600 + 30 * 60 + 1) * 1_000_000_000;
        rows.push(FakeTradeRow::new(
            base,
            u64::from(day_offset) + 1,
            "100.02",
            20,
            "XNYS",
        ));
    }
    rows
}

/// Three one-minute SPY bars inside the default study window.
fn spy_bar_rows() -> Vec<FakeBarRow> {
    use backtest::backtest::errors::ONE_DAY_NS;
    use marketdata::Date;

    let date = Date::from_ymd(2024, 1, 2).expect("date");
    let base = i64::from(date.days()) * ONE_DAY_NS + (14 * 3_600 + 30 * 60) * 1_000_000_000;
    vec![
        FakeBarRow::new(base, "100.00", "100.50", "99.50", "100.25", "1000.00"),
        FakeBarRow::new(
            base + 60 * 1_000_000_000,
            "100.25",
            "101.00",
            "100.00",
            "100.75",
            "1200.00",
        ),
        FakeBarRow::new(
            base + 120 * 1_000_000_000,
            "100.75",
            "101.25",
            "100.50",
            "101.00",
            "800.00",
        ),
    ]
}

/// Point the process lake environment at one fake route server.
///
/// The environment is process-wide, so the shared test lock serializes the
/// tests that mutate it and the guard restores the prior values on drop.
struct LakeEnvGuard {
    _guard: std::sync::MutexGuard<'static, ()>,
    _server: FakeLakeServer,
    saved: [(&'static str, Option<String>); 3],
}

impl LakeEnvGuard {
    fn set() -> Self {
        let guard = crate::tests::LAKE_ENV_TEST_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let saved = [
            ("LAKE_API_URL", std::env::var("LAKE_API_URL").ok()),
            ("LAKE_API_BASE_URL", std::env::var("LAKE_API_BASE_URL").ok()),
            (
                "LAKE_API__BEARER_TOKEN",
                std::env::var("LAKE_API__BEARER_TOKEN").ok(),
            ),
        ];
        let server = FakeLakeServer::start(FakeLakeConfig {
            read_bodies_by_type: std::collections::BTreeMap::from([
                ("quote".to_string(), quote_stream("SPY", &spy_quote_rows())),
                ("trade".to_string(), trade_stream("SPY", &spy_trade_rows())),
                ("bars".to_string(), bars_stream("SPY", &spy_bar_rows())),
            ]),
            ..FakeLakeConfig::new("local-submitter-token")
        });
        std::env::set_var("LAKE_API_URL", server.base_url());
        std::env::set_var("LAKE_API__BEARER_TOKEN", "local-submitter-token");
        Self {
            _guard: guard,
            _server: server,
            saved,
        }
    }
}

impl Drop for LakeEnvGuard {
    fn drop(&mut self) {
        for (name, value) in &self.saved {
            match value {
                Some(value) => std::env::set_var(name, value),
                None => std::env::remove_var(name),
            }
        }
    }
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
    let _lake = LakeEnvGuard::set();
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
    let _lake = LakeEnvGuard::set();
    let study_source = r#"
use harness_strategy_sdk::{export_study_program, BarEvent, MetricPublisher, StudyProgram};
use harness_strategy_sdk::serde_json::Value;

/// Count the observed bars and publish the mean bar close.
struct BarObserver {
    bars: u64,
    close_sum: f64,
    last_close: f64,
}

impl StudyProgram for BarObserver {
    fn new(_parameters: Value) -> Self {
        Self { bars: 0, close_sum: 0.0, last_close: 0.0 }
    }

    fn on_bar(&mut self, event: &BarEvent) {
        self.bars += 1;
        if let Ok(close) = event.close.to_string().parse::<f64>() {
            self.close_sum += close;
            self.last_close = close;
        }
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("bar_observations", self.bars as f64);
        if self.bars > 0 {
            let _ = metrics.publish("mean_close", self.close_sum / self.bars as f64);
            let _ = metrics.publish("last_close", self.last_close);
        }
    }
}

export_study_program!(BarObserver);
"#;
    let digest = backtest::sha256::digest_hex(study_source.as_bytes());
    let document = json!({
        "schema_version": 1,
        "shell": "study",
        "run_id": "workspace-research-metric",
        "seed": 1729,
        "data": {"symbols": ["SPY"], "bar_interval": "1min"},
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
        .get("bar_observations")
        .and_then(|value| value.parse::<f64>().ok())
        .expect("custom metric");
    assert_eq!(observed, 3.0, "the three served bars must fold");
    assert!(record
        .metrics
        .get("mean_close")
        .and_then(|value| value.parse::<f64>().ok())
        .is_some());
    let stored = run_store.fetch(run_id).expect("stored run");
    assert!(
        stored.artifacts.contains_key("study.json"),
        "artifacts: {:?}",
        stored.artifacts.keys().collect::<Vec<_>>()
    );
    let metrics = stored
        .artifacts
        .get("metrics.json")
        .expect("metrics artifact");
    let document: Value = serde_json::from_slice(metrics).expect("metrics json");
    assert_eq!(
        document["custom"]["bar_observations"]
            .as_str()
            .and_then(|value| value.parse::<f64>().ok()),
        Some(observed)
    );
    assert!(stored.artifacts.contains_key("artifacts/emissions.parquet"));
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_study_runtime_rejects_tick_events_through_the_engine() {
    use backtest::backtest::engine_core::{
        build_default_manifest, run_native_engine, EngineOptions,
    };
    use backtest::backtest::errors::{BacktestError, ONE_DAY_NS};
    use backtest::backtest::fixture::{
        ExchangeSession, MarketEvent, ReplayFingerprints, ReplayFixture, SyntheticDataOptions,
    };
    use backtest::backtest::preflight::preflight_data;
    use harness_strategy_jit::{study_instance, ManagedWorkspace, STUDY_BARS_ONLY};
    use marketdata::Date;

    let directory = std::env::temp_dir().join(format!(
        "harness-study-tick-reject-{}-{}",
        std::process::id(),
        Utc::now().timestamp_nanos_opt().unwrap_or(0)
    ));
    fs::create_dir_all(&directory).expect("dir");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");

    let source = r#"
use harness_strategy_sdk::{export_study_program, BarEvent, StudyProgram};
use harness_strategy_sdk::serde_json::Value;

struct Bars;
impl StudyProgram for Bars {
    fn new(_parameters: Value) -> Self { Self }
    fn on_bar(&mut self, _event: &BarEvent) {}
}
export_study_program!(Bars);
"#;
    let runtime = study_instance(&workspace, source, &serde_json::json!({}), "tick-study")
        .expect("study instance");

    let date = Date::from_ymd(2024, 1, 2).expect("date");
    let open_ns = i64::from(date.days()) * ONE_DAY_NS + 14 * 3_600 * 1_000_000_000;
    let end_ns = open_ns + 60 * 1_000_000_000;
    let mut configuration =
        backtest::backtest::config::BacktestConfig::new("tick-reject", open_ns, end_ns, 1)
            .expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    let events = vec![MarketEvent::Trade {
        event_id: "t-1".to_string(),
        timestamp_ns: open_ns + 1_000_000_000,
        sequence: 1,
        symbol: "SPY".to_string(),
        price: num::Decimal18::from_i64(450),
        quantity: num::Decimal18::from_i64(10),
        venue: "XNYS".to_string(),
        trade_id: "1".to_string(),
    }];
    let fingerprints = ReplayFingerprints::of_events(&events);
    let fixture = ReplayFixture {
        sessions: vec![ExchangeSession {
            session_date_days: i64::from(date.days()),
            open_ns: open_ns - 3_600 * 1_000_000_000,
            close_ns: end_ns,
        }],
        events,
        source_id: "tick-fixture".to_string(),
        symbols: vec!["SPY".to_string()],
        fingerprints,
        expected_data_fingerprint: None,
    };
    let preflight = preflight_data(&configuration, &fixture).expect("preflight");
    let manifest = build_default_manifest(&configuration, &fixture, &preflight).expect("manifest");

    let error = run_native_engine(
        &configuration,
        &fixture,
        &preflight,
        manifest,
        &EngineOptions::default(),
        vec![runtime],
        &SyntheticDataOptions::default(),
    )
    .expect_err("a tick must never reach a study runtime");
    match error {
        BacktestError::StrategyExecution(message) => assert_eq!(message, STUDY_BARS_ONLY),
        other => panic!("expected a strategy execution error, got {other:?}"),
    }
    let _ = fs::remove_dir_all(&directory);
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
