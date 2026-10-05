//! Managed-workspace compilation behavior.

use std::path::PathBuf;

use backtest::backtest::engine_core::{EngineBar, EngineBarFrame, EngineMarket, StrategyRuntime};
use harness_strategy_jit::{compile, JitStudyRuntime, ManagedWorkspace, ProgramKind};
use harness_strategy_sdk::Decimal;

const STUDY_SOURCE: &str = include_str!("fixtures/study_program.rs");

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-strategy-jit-compile-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

#[test]
fn compile_failures_report_the_build_error() {
    let directory = temp_dir("failure");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let error =
        compile(&workspace, "this is not rust", ProgramKind::Study).expect_err("must not compile");
    match error {
        harness_strategy_jit::JitError::Compile {
            detail,
            diagnostics,
            ..
        } => {
            assert!(
                !diagnostics.is_empty(),
                "authored error must carry rustc JSON diagnostics"
            );
            assert_eq!(diagnostics[0]["level"], "error");
            assert!(
                detail.contains("error"),
                "expected the compiler detail, got {detail}"
            );
        }
        other => panic!("expected a compile failure, got {other:?}"),
    }
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn an_empty_source_is_rejected_before_any_build() {
    let directory = temp_dir("empty-source");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let error = compile(&workspace, "   \n", ProgramKind::Strategy).expect_err("empty source");
    assert!(
        matches!(error, harness_strategy_jit::JitError::Invalid(_)),
        "expected an invalid-source error, got {error:?}"
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn the_workspace_records_provenance_for_every_artifact() {
    let directory = temp_dir("manifest");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let compiled = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("compile");
    let entries = workspace.read_manifest();
    let entry = entries.get(&compiled.digest).expect("manifest entry");
    assert_eq!(entry.kind, "study");
    assert_eq!(entry.crate_name, compiled.crate_name);
    assert_eq!(entry.toolchain_digest, compiled.toolchain_digest);
    assert_eq!(entry.sdk_version, harness_strategy_sdk::SDK_VERSION);
    assert_eq!(entry.sdk_abi_version, harness_strategy_sdk::SDK_ABI_VERSION);
    assert!(workspace
        .target_dir()
        .join(&entry.artifact_relpath)
        .is_file());
    let exact = harness_strategy_sdk::serde_json::json!({
        "open": "1.500", "high": "2.000", "low": "-0", "close": "1E+2", "volume": "1E-9"
    });
    let parameters = harness_strategy_sdk::serde_json::json!({"expected": [exact.clone(), exact]});
    let mut runtime = JitStudyRuntime::from_source(&workspace, STUDY_SOURCE, &parameters)
        .expect("load cached study");
    let bar = |symbol: &str| EngineBar {
        event_id: format!("bar-{symbol}"),
        timestamp_ns: 10,
        sequence: 1,
        symbol: symbol.to_string(),
        open: "1.500".parse().unwrap(),
        high: "2.000".parse().unwrap(),
        low: "-0".parse().unwrap(),
        close: "1E+2".parse().unwrap(),
        volume: "0.000000001".parse().unwrap(),
    };
    let frame = EngineBarFrame {
        interval: "1min".into(),
        interval_start_ns: 10,
        interval_end_ns: 20,
        available_at_ns: 25,
        bars_by_symbol: [
            ("AAA".to_string(), bar("AAA")),
            ("BBB".to_string(), bar("BBB")),
        ]
        .into(),
        missing_symbols: vec!["CCC".to_string()],
    };
    StrategyRuntime::on_bar_frame(&mut runtime, &frame).expect("one atomic callback");
    let invalid = EngineBarFrame {
        available_at_ns: 19,
        ..frame.clone()
    };
    assert!(runtime.on_bar_frame(&invalid).is_err());
    let invalid_key = EngineBarFrame {
        bars_by_symbol: [("WRONG".to_string(), bar("AAA"))].into(),
        ..frame.clone()
    };
    assert!(runtime.on_bar_frame(&invalid_key).is_err());
    assert!(StrategyRuntime::on_bar(&mut runtime, &bar("AAA")).is_err());
    assert!(runtime.on_market(&EngineMarket::Bar(bar("AAA"))).is_err());
    let quote = EngineMarket::Quote {
        event_id: "quote".into(),
        timestamp_ns: 10,
        sequence: 1,
        symbol: "AAA".into(),
        bid: Decimal::ZERO,
        ask: Decimal::ZERO,
        bid_quantity: Decimal::ZERO,
        ask_quantity: Decimal::ZERO,
        venue: "test".into(),
    };
    assert!(runtime.on_market(&quote).unwrap().is_empty());
    let trade = EngineMarket::Trade {
        event_id: "trade".into(),
        timestamp_ns: 10,
        sequence: 1,
        symbol: "AAA".into(),
        price: Decimal::ZERO,
        quantity: Decimal::ZERO,
        venue: "test".into(),
        trade_id: "trade".into(),
    };
    assert!(runtime.on_market(&trade).unwrap().is_empty());
    runtime.on_stop().expect("publish metrics");
    let metrics = runtime.published_metrics();
    assert_eq!(metrics["study_frame_count"], "1");
    assert_eq!(metrics["study_bar_count"], "2");
    assert_eq!(metrics["study_missing_count"], "1");
    assert_eq!(metrics["study_bar_mismatches"], "0");
    drop(runtime);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn persisted_raw_bar_study_source_fails_to_build() {
    let directory = temp_dir("old-study-source");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let old_source = STUDY_SOURCE.replace(
        "fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext)",
        "fn on_bar_frame(&mut self, frame: &BarFrame)",
    );
    let error =
        compile(&workspace, &old_source, ProgramKind::Study).expect_err("old callback rejected");
    match error {
        harness_strategy_jit::JitError::Compile { detail, .. } => {
            assert!(
                detail.contains("on_bar_frame") && detail.contains("not a member"),
                "{detail}"
            );
        }
        other => panic!("expected compile failure, got {other:?}"),
    }
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn a_matching_manifest_is_a_cache_hit() {
    let directory = temp_dir("cache-hit");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let first = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("initial compile");
    assert!(!first.cached);
    let second = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("cached compile");
    assert!(second.cached);
    assert_eq!(second.digest, first.digest);
    assert_eq!(second.artifact_path, first.artifact_path);
    assert_eq!(
        second.sdk_version,
        harness_strategy_sdk::SDK_VERSION.to_string()
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn an_older_manifest_abi_version_forces_a_rebuild() {
    let directory = temp_dir("stale-abi");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let first = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("initial compile");
    assert!(!first.cached);

    let mut entries = workspace.read_manifest();
    let entry = entries.get_mut(&first.digest).expect("manifest entry");
    entry.sdk_abi_version = harness_strategy_sdk::SDK_ABI_VERSION - 1;
    workspace.write_manifest(&entries).expect("manifest write");

    let rebuilt = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("rebuild");
    assert!(!rebuilt.cached);
    assert_eq!(rebuilt.digest, first.digest);
    let entries = workspace.read_manifest();
    let entry = entries.get(&first.digest).expect("manifest entry");
    assert_eq!(entry.sdk_abi_version, harness_strategy_sdk::SDK_ABI_VERSION);
    let _ = std::fs::remove_dir_all(&directory);
}
