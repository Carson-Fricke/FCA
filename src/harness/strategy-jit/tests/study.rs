//! Phase N1 proof: authored Rust programs run through the engine from
//! JIT-loaded dylibs.
//!
//! Each test materializes the two-event Parquet lake fixture, JIT-compiles an
//! authored source into the managed workspace, loads the cdylib, and drives
//! the real engine job seam — the study through
//! [`run_native_study_job_with_data_dir`], the strategy through
//! [`run_native_job_with_data_dir`]. The study asserts the capability
//! contract structurally: the runtime returns no orders and records the
//! authored program's published metrics in `study.json`.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use backtest::backtest::config::{BacktestConfig, StrategyInstanceConfig};
use backtest::backtest::engine_core::EngineOptions;
use backtest::backtest::errors::ONE_DAY_NS;
use backtest::backtest::job::{run_native_job_with_data_dir, run_native_study_job_with_data_dir};
use harness_strategy_jit::{
    compile, strategy_instance, study_instance, ManagedWorkspace, ProgramKind,
};
use marketdata::events::{
    MarketEvent as LakeEvent, QuoteEvent as LakeQuote, TradeEvent as LakeTrade,
};
use marketdata::time::DateTime;
use marketdata::{write_lake_partition, AssetClass, DataType, Date};

const STUDY_SOURCE: &str = include_str!("fixtures/study_program.rs");
const STRATEGY_SOURCE: &str = include_str!("fixtures/strategy_program.rs");

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

/// Materialize the two-event lake fixture and return the run window.
fn materialize_lake(lake: &Path) -> (i64, i64) {
    let date = Date::from_ymd(2024, 1, 2).unwrap();
    let day = i64::from(date.days());
    let start_ns = day * ONE_DAY_NS + (14 * 3_600 + 30 * 60) * 1_000_000_000;
    let end_ns = (day + 1) * ONE_DAY_NS;

    let quote = LakeEvent::Quote(LakeQuote {
        event_id: "q-1".to_string(),
        timestamp: DateTime::from_micros(start_ns / 1_000),
        sequence: 0,
        symbol: "SPY".to_string(),
        bid_price: "450.00".parse().unwrap(),
        ask_price: "450.02".parse().unwrap(),
        bid_size: "100".parse().unwrap(),
        ask_size: "100".parse().unwrap(),
        venue: Some("XNYS".to_string()),
    });
    let trade = LakeEvent::Trade(LakeTrade {
        event_id: "t-1".to_string(),
        timestamp: DateTime::from_micros(start_ns / 1_000 + 1_000_000),
        sequence: 1,
        symbol: "SPY".to_string(),
        price: "450.01".parse().unwrap(),
        quantity: "50".parse().unwrap(),
        venue: Some("XNYS".to_string()),
        trade_id: Some("tid-1".to_string()),
    });
    write_lake_partition(
        lake,
        date,
        AssetClass::Equity,
        DataType::Quote,
        "SPY",
        &[quote],
    )
    .expect("write quote lake");
    write_lake_partition(
        lake,
        date,
        AssetClass::Equity,
        DataType::Trade,
        "SPY",
        &[trade],
    )
    .expect("write trade lake");
    (start_ns, end_ns)
}

fn study_configuration(run_id: &str, start_ns: i64, end_ns: i64, digest: &str) -> BacktestConfig {
    let mut configuration = BacktestConfig::new(run_id, start_ns, end_ns, 7).expect("config");
    configuration.shell = "study".to_string();
    configuration.symbols = vec!["SPY".to_string()];
    configuration.strategy_instances = vec![StrategyInstanceConfig {
        instance_id: "jit-study".to_string(),
        implementation: format!("jit:{digest}"),
        bundle_hash: digest.to_string(),
        parameters: BTreeMap::new(),
        symbols: vec!["SPY".to_string()],
        capital_fraction: "1.0".to_string(),
    }];
    configuration
}

#[test]
fn runs_a_jit_authored_study_through_the_engine() {
    let directory = temp_dir("study");
    let workspace_root = directory.join("jit");
    let lake = directory.join("lake");
    let output = directory.join("out");
    let (start_ns, end_ns) = materialize_lake(&lake);
    let workspace = ManagedWorkspace::open(&workspace_root).expect("workspace");

    let compiled = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("compile study");
    let configuration = study_configuration("jit-study", start_ns, end_ns, &compiled.digest);
    let programs = vec![study_instance(
        &workspace,
        STUDY_SOURCE,
        &serde_json::json!({}),
        "jit-study",
    )
    .expect("study instance")];

    let (outcome, artifact_path) = run_native_study_job_with_data_dir(
        &configuration,
        &EngineOptions::default(),
        Some(output.as_path()),
        programs,
        &lake,
    )
    .expect("native study job");

    // The engine consumed the two real lake events through the JIT-loaded
    // dylib, the study never submitted orders, and its metrics landed in the
    // run outcome.
    assert!(outcome.completed());
    assert_eq!(outcome.market_event_count, 2);
    assert_eq!(outcome.order_count, 0);
    assert!(outcome.fills.is_empty());
    assert_eq!(
        outcome
            .custom_metrics
            .get("study_quote_count")
            .map(String::as_str),
        Some("1")
    );
    assert_eq!(
        outcome
            .custom_metrics
            .get("study_trade_count")
            .map(String::as_str),
        Some("1")
    );
    assert_eq!(
        outcome
            .custom_metrics
            .get("study_last_mid")
            .map(String::as_str),
        Some("450.01")
    );

    // The run recorded the authored program's provenance and wrote the
    // canonical study artifact.
    let document = std::fs::read_to_string(&artifact_path).expect("artifact");
    assert!(document.contains("\"market_events\":2"), "{document}");
    assert!(document.contains(&compiled.digest), "{document}");
    assert!(document.contains("study_quote_count"), "{document}");
    assert!(output.join("metrics.json").exists(), "metrics.json missing");
    assert!(output.join("report.html").exists(), "report.html missing");
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn runs_a_jit_authored_strategy_through_the_engine() {
    let directory = temp_dir("strategy");
    let workspace_root = directory.join("jit");
    let lake = directory.join("lake");
    let output = directory.join("out");
    let (start_ns, end_ns) = materialize_lake(&lake);
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

    let (outcome, _artifact_path) = run_native_job_with_data_dir(
        &configuration,
        &EngineOptions::default(),
        Some(output.as_path()),
        strategies,
        &lake,
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

#[test]
fn a_panic_in_an_authored_program_fails_the_engine_run() {
    let directory = temp_dir("panic");
    let workspace_root = directory.join("jit");
    let lake = directory.join("lake");
    let (start_ns, end_ns) = materialize_lake(&lake);
    let workspace = ManagedWorkspace::open(&workspace_root).expect("workspace");

    let source = r#"
use harness_strategy_sdk::{export_study_program, QuoteEvent, StudyProgram};
use harness_strategy_sdk::serde_json::Value;

struct Boom;

impl StudyProgram for Boom {
    fn new(_parameters: Value) -> Self { Self }
    fn on_quote(&mut self, _event: &QuoteEvent) {
        panic!("authored program panicked");
    }
}

export_study_program!(Boom);
"#;
    let configuration = study_configuration("jit-panic", start_ns, end_ns, "digest-panic");
    let programs = vec![
        study_instance(&workspace, source, &serde_json::json!({}), "jit-panic")
            .expect("study instance"),
    ];
    let error = run_native_study_job_with_data_dir(
        &configuration,
        &EngineOptions::default(),
        None,
        programs,
        &lake,
    )
    .expect_err("a panicking program must fail the run");
    let message = error.to_string();
    assert!(
        message.contains("authored program panicked"),
        "expected the panic detail, got {message}"
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn the_study_runtime_returns_no_orders_by_construction() {
    // The capability gate from the Python surface, moved to the SDK: even a
    // program that would submit orders cannot, because the study runtime has
    // no order path.
    let directory = temp_dir("capability");
    let workspace_root = directory.join("jit");
    let lake = directory.join("lake");
    let (start_ns, end_ns) = materialize_lake(&lake);
    let workspace = ManagedWorkspace::open(&workspace_root).expect("workspace");

    let source = r#"
use harness_strategy_sdk::{
    export_study_program, MetricPublisher, OrderSink, QuoteEvent, StudyProgram,
};
use harness_strategy_sdk::serde_json::Value;

struct OrderHungry;

impl StudyProgram for OrderHungry {
    fn new(_parameters: Value) -> Self { Self }
    fn on_quote(&mut self, _event: &QuoteEvent) {}
}

export_study_program!(OrderHungry);
"#;
    let compiled = compile(&workspace, source, ProgramKind::Study).expect("compile");
    let configuration = study_configuration("jit-capability", start_ns, end_ns, &compiled.digest);
    let programs =
        vec![
            study_instance(&workspace, source, &serde_json::json!({}), "jit-capability")
                .expect("study instance"),
        ];
    let (outcome, _artifact) = run_native_study_job_with_data_dir(
        &configuration,
        &EngineOptions::default(),
        None,
        programs,
        &lake,
    )
    .expect("capability run");
    assert_eq!(outcome.order_count, 0);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn a_rejected_metric_is_recorded_as_a_teardown_error() {
    let directory = temp_dir("metric");
    let workspace_root = directory.join("jit");
    let lake = directory.join("lake");
    let (start_ns, end_ns) = materialize_lake(&lake);
    let workspace = ManagedWorkspace::open(&workspace_root).expect("workspace");

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
    let configuration = study_configuration("jit-reserved", start_ns, end_ns, "digest-reserved");
    let programs = vec![
        study_instance(&workspace, source, &serde_json::json!({}), "jit-reserved")
            .expect("study instance"),
    ];
    let (outcome, _artifact) = run_native_study_job_with_data_dir(
        &configuration,
        &EngineOptions::default(),
        None,
        programs,
        &lake,
    )
    .expect("study job");
    // The engine records on-stop failures as teardown errors rather than
    // aborting the run, exactly as it does for the Python adapter; the
    // rejected metric must not appear in the published metrics.
    assert!(
        outcome
            .teardown_errors
            .iter()
            .any(|error| error.contains("reserved by the harness")),
        "expected the reservation detail, got {:?}",
        outcome.teardown_errors
    );
    assert!(
        outcome.custom_metrics.is_empty(),
        "{:?}",
        outcome.custom_metrics
    );
    let _ = std::fs::remove_dir_all(&directory);
}
