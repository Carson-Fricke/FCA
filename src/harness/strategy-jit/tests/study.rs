//! Authored Rust programs through the JIT seam: studies fold bars, strategies
//! fold ticks.
//!
//! Each study test JIT-compiles an authored source into the managed workspace,
//! loads the cdylib through [`JitStudyRuntime`], and drives the explicit bar
//! host entry — the engine's bar arm lands next wave, so bars are fed directly
//! here. The contract is bars-only: tick events are rejected with a typed
//! error, the study vtable carries no order path, and the strategy path still
//! runs through the real engine job seam end to end. No network and no lake
//! files are touched by the study tests.

use std::collections::BTreeMap;
use std::path::PathBuf;

use backtest::backtest::config::{BacktestConfig, StrategyInstanceConfig};
use backtest::backtest::engine_core::{EngineMarket, EngineOptions, StrategyRuntime};
use backtest::backtest::errors::{BacktestError, ONE_DAY_NS};
use backtest::backtest::job::run_native_job_with_lake_client;
use harness_lakeclient::testing::{
    quote_stream, trade_stream, FakeLakeConfig, FakeLakeServer, FakeQuoteRow, FakeTradeRow,
};
use harness_lakeclient::{BlockingLakeClient, LakeClientConfig};
use harness_strategy_jit::{
    compile, strategy_instance, HostBar, JitStudyRuntime, ManagedWorkspace, ProgramKind,
    STUDY_BARS_ONLY,
};
use marketdata::Date;

const STUDY_SOURCE: &str = include_str!("fixtures/study_program.rs");
const STRATEGY_SOURCE: &str = include_str!("fixtures/strategy_program.rs");
const TOKEN: &str = "strategy-jit-lake-token";

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-strategy-jit-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

/// The exact decimal literals each fixture bar must surface, trailing zeros
/// included; the authored fixture compares them host-pinned.
fn expected_parameters() -> serde_json::Value {
    serde_json::json!({
        "expected": [
            {
                "open": "450.00",
                "high": "451.250",
                "low": "449.750",
                "close": "450.50",
                "volume": "1000.00",
            },
            {
                "open": "450.50",
                "high": "452.00",
                "low": "450.25",
                "close": "450.01",
                "volume": "12345.00",
            },
        ]
    })
}

/// The two-bar stream every study test folds through the host entry.
fn bars() -> Vec<HostBar<'static>> {
    vec![
        HostBar {
            event_id: "bar-1",
            timestamp_ns: 1_704_206_400_000_000_000,
            sequence: 1,
            symbol: "SPY",
            open: "450.00".parse().expect("decimal literal"),
            high: "451.250".parse().expect("decimal literal"),
            low: "449.750".parse().expect("decimal literal"),
            close: "450.50".parse().expect("decimal literal"),
            volume: "1000.00".parse().expect("decimal literal"),
        },
        HostBar {
            event_id: "bar-2",
            timestamp_ns: 1_704_206_460_000_000_000,
            sequence: 2,
            symbol: "SPY",
            open: "450.50".parse().expect("decimal literal"),
            high: "452.00".parse().expect("decimal literal"),
            low: "450.25".parse().expect("decimal literal"),
            close: "450.01".parse().expect("decimal literal"),
            volume: "12345.00".parse().expect("decimal literal"),
        },
    ]
}

fn sample_bar() -> HostBar<'static> {
    HostBar {
        event_id: "bar-1",
        timestamp_ns: 1_704_206_400_000_000_000,
        sequence: 1,
        symbol: "SPY",
        open: "450.00".parse().expect("decimal literal"),
        high: "451.25".parse().expect("decimal literal"),
        low: "449.75".parse().expect("decimal literal"),
        close: "450.01".parse().expect("decimal literal"),
        volume: "1000.00".parse().expect("decimal literal"),
    }
}

/// Start the two-event lake fixture and return the run window plus its client.
fn fake_lake() -> (FakeLakeServer, BlockingLakeClient, i64, i64) {
    let date = Date::from_ymd(2024, 1, 2).unwrap();
    let day = i64::from(date.days());
    let start_ns = day * ONE_DAY_NS + (14 * 3_600 + 30 * 60) * 1_000_000_000;
    let end_ns = (day + 1) * ONE_DAY_NS;

    let quotes = vec![FakeQuoteRow::new(
        start_ns, "450.00", "450.02", 100, 100, "XNYS",
    )];
    let trades = vec![FakeTradeRow::new(
        start_ns + 1_000_000_000,
        41,
        "450.01",
        25,
        "XNYS",
    )];
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([
            ("quote".to_string(), quote_stream("SPY", &quotes)),
            ("trade".to_string(), trade_stream("SPY", &trades)),
        ]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = BlockingLakeClient::new(
        LakeClientConfig::new(server.base_url(), TOKEN).expect("client config"),
    )
    .expect("blocking client");
    (server, client, start_ns, end_ns)
}

/// Compile the source in a fresh workspace, fold the bar stream, and stop.
fn run_study(
    name: &str,
    source: &str,
    parameters: &serde_json::Value,
) -> (BTreeMap<String, String>, String) {
    let directory = temp_dir(name);
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let compiled = compile(&workspace, source, ProgramKind::Study).expect("compile study");
    let mut runtime =
        JitStudyRuntime::from_source(&workspace, source, parameters).expect("study runtime");
    for bar in &bars() {
        runtime.on_bar(bar).expect("bar folds");
    }
    runtime.on_stop().expect("stop publishes");
    let metrics = runtime.published_metrics();
    let _ = std::fs::remove_dir_all(&directory);
    (metrics, compiled.digest)
}

#[test]
fn an_authored_bar_program_receives_exact_bar_values_and_publishes_metrics() {
    let directory = temp_dir("bars");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");

    let mut runtime =
        JitStudyRuntime::from_source(&workspace, STUDY_SOURCE, &expected_parameters())
            .expect("study runtime");
    for bar in &bars() {
        runtime.on_bar(bar).expect("bar folds");
    }
    runtime.on_stop().expect("stop publishes");

    let metrics = runtime.published_metrics();
    // The authored program compared every bar field string-exactly against the
    // pinned literals, so zero mismatches means the full scale crossed intact.
    assert_eq!(
        metrics.get("study_bar_count").map(String::as_str),
        Some("2")
    );
    assert_eq!(
        metrics.get("study_bar_mismatches").map(String::as_str),
        Some("0"),
        "{metrics:?}"
    );
    // The last bar's reads round-trip through the metric sink.
    assert_eq!(
        metrics.get("study_last_open").map(String::as_str),
        Some("450.5")
    );
    assert_eq!(
        metrics.get("study_last_high").map(String::as_str),
        Some("452")
    );
    assert_eq!(
        metrics.get("study_last_low").map(String::as_str),
        Some("450.25")
    );
    assert_eq!(
        metrics.get("study_last_close").map(String::as_str),
        Some("450.01")
    );
    assert_eq!(
        metrics.get("study_last_volume").map(String::as_str),
        Some("12345")
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn a_study_rerun_over_the_same_bars_is_byte_identical() {
    let parameters = expected_parameters();
    let (first_metrics, first_digest) = run_study("rerun-a", STUDY_SOURCE, &parameters);
    let (second_metrics, second_digest) = run_study("rerun-b", STUDY_SOURCE, &parameters);
    assert_eq!(first_digest, second_digest, "source digest must be stable");
    let first = serde_json::to_string(&first_metrics).expect("serialize metrics");
    let second = serde_json::to_string(&second_metrics).expect("serialize metrics");
    assert_eq!(first, second, "published metrics must be byte-identical");
}

#[test]
fn a_panic_in_an_authored_bar_callback_fails_the_host_entry() {
    let directory = temp_dir("panic");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");

    let source = r#"
use harness_strategy_sdk::{export_study_program, BarEvent, StudyProgram};
use harness_strategy_sdk::serde_json::Value;

struct Boom;

impl StudyProgram for Boom {
    fn new(_parameters: Value) -> Self { Self }
    fn on_bar(&mut self, _event: &BarEvent) {
        panic!("authored program panicked");
    }
}

export_study_program!(Boom);
"#;
    let mut runtime = JitStudyRuntime::from_source(&workspace, source, &serde_json::json!({}))
        .expect("study runtime");
    let error = runtime
        .on_bar(&sample_bar())
        .expect_err("a panicking program must fail the host entry");
    match error {
        BacktestError::StrategyExecution(message) => assert!(
            message.contains("authored program panicked"),
            "expected the panic detail, got {message}"
        ),
        other => panic!("expected a strategy execution error, got {other:?}"),
    }
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn a_rejected_metric_is_recorded_as_a_stop_error() {
    let directory = temp_dir("metric");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");

    let source = r#"
use harness_strategy_sdk::{export_study_program, MetricPublisher, StudyProgram};
use harness_strategy_sdk::serde_json::Value;

struct ReservedMetric;

impl StudyProgram for ReservedMetric {
    fn new(_parameters: Value) -> Self { Self }
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        // The author ignores the error; the host sink still rejects it and
        // the run must fail.
        let _ = metrics.publish("total_return", 1.0);
    }
}

export_study_program!(ReservedMetric);
"#;
    let mut runtime = JitStudyRuntime::from_source(&workspace, source, &serde_json::json!({}))
        .expect("study runtime");
    runtime.on_bar(&sample_bar()).expect("bar folds");
    let error = runtime
        .on_stop()
        .expect_err("a reserved metric must fail the stop");
    match error {
        BacktestError::StrategyExecution(message) => assert!(
            message.contains("reserved by the harness"),
            "expected the reservation detail, got {message}"
        ),
        other => panic!("expected a strategy execution error, got {other:?}"),
    }
    assert!(
        runtime.published_metrics().is_empty(),
        "a rejected metric must not be published"
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn tick_events_fail_loud_for_study_runtimes() {
    let directory = temp_dir("ticks");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let mut runtime =
        JitStudyRuntime::from_source(&workspace, STUDY_SOURCE, &serde_json::json!({}))
            .expect("study runtime");

    let quote = EngineMarket::Quote {
        event_id: "quote-1".to_string(),
        timestamp_ns: 1_704_206_400_000_000_000,
        sequence: 1,
        symbol: "SPY".to_string(),
        bid: "450.00".parse().expect("decimal literal"),
        ask: "450.02".parse().expect("decimal literal"),
        bid_quantity: "100".parse().expect("decimal literal"),
        ask_quantity: "100".parse().expect("decimal literal"),
        venue: "XNYS".to_string(),
    };
    let error = runtime
        .on_market(&quote)
        .expect_err("a quote must never be silently ignored");
    match error {
        BacktestError::StrategyExecution(message) => assert_eq!(message, STUDY_BARS_ONLY),
        other => panic!("expected a strategy execution error, got {other:?}"),
    }

    let trade = EngineMarket::Trade {
        event_id: "trade-1".to_string(),
        timestamp_ns: 1_704_206_401_000_000_000,
        sequence: 2,
        symbol: "SPY".to_string(),
        price: "450.01".parse().expect("decimal literal"),
        quantity: "25".parse().expect("decimal literal"),
        venue: "XNYS".to_string(),
        trade_id: "trade-1".to_string(),
    };
    let error = runtime
        .on_market(&trade)
        .expect_err("a trade must never be synthesized into a bar");
    match error {
        BacktestError::StrategyExecution(message) => assert_eq!(message, STUDY_BARS_ONLY),
        other => panic!("expected a strategy execution error, got {other:?}"),
    }

    assert!(
        runtime.published_metrics().is_empty(),
        "a rejected tick must not fold"
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn runs_a_jit_authored_strategy_through_the_engine() {
    let directory = temp_dir("strategy");
    let workspace_root = directory.join("jit");
    let output = directory.join("out");
    let (_server, client, start_ns, end_ns) = fake_lake();
    let workspace = ManagedWorkspace::open(&workspace_root).expect("workspace");

    let compiled =
        compile(&workspace, STRATEGY_SOURCE, ProgramKind::Strategy).expect("compile strategy");
    let mut configuration =
        BacktestConfig::new("jit-strategy", start_ns, end_ns, 7).expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    configuration.strategy_instances = vec![StrategyInstanceConfig {
        instance_id: "jit-strategy".to_string(),
        implementation: format!("jit:{}", compiled.digest),
        bundle_hash: compiled.digest.clone(),
        parameters: BTreeMap::new(),
        symbols: vec!["SPY".to_string()],
        capital_fraction: "1.0".to_string(),
    }];
    let strategies = vec![strategy_instance(
        &workspace,
        STRATEGY_SOURCE,
        &serde_json::json!({}),
        "jit-strategy",
    )
    .expect("strategy instance")];

    let (outcome, _artifact_path) = run_native_job_with_lake_client(
        &configuration,
        &EngineOptions::default(),
        Some(output.as_path()),
        strategies,
        &client,
    )
    .expect("native strategy job");

    // The JIT-loaded strategy submitted one order through the host sink, the
    // venue filled it, and the program observed the fill.
    assert!(outcome.completed());
    assert_eq!(outcome.market_event_count, 2);
    assert_eq!(outcome.order_count, 1);
    assert_eq!(outcome.fills.len(), 1);
    assert_eq!(
        outcome
            .custom_metrics
            .get("jit_submitted")
            .map(String::as_str),
        Some("1")
    );
    assert_eq!(
        outcome.custom_metrics.get("jit_filled").map(String::as_str),
        Some("1")
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn jit_compilation_is_cached_by_source_digest() {
    let directory = temp_dir("cache");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let first = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("first compile");
    assert!(!first.cached);
    assert!(first.artifact_path.is_file());
    let second = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("second compile");
    assert!(second.cached, "the second compile must reuse the artifact");
    assert_eq!(first.digest, second.digest);
    assert_eq!(first.artifact_path, second.artifact_path);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn a_source_without_sdk_exports_fails_to_load() {
    let directory = temp_dir("no-exports");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let compiled = compile(&workspace, "pub fn nothing() {}\n", ProgramKind::Study)
        .expect("compile without exports");
    let error = match unsafe {
        harness_strategy_jit::LoadedStudy::load(&compiled.artifact_path, &serde_json::json!({}))
    } {
        Err(error) => error,
        Ok(_loaded) => panic!("missing exports must fail the load"),
    };
    assert!(
        matches!(error, harness_strategy_jit::JitError::Export { ref symbol, .. } if symbol == "harness_sdk_abi_version"),
        "expected a missing-export error, got {error:?}"
    );
    let _ = std::fs::remove_dir_all(&directory);
}
