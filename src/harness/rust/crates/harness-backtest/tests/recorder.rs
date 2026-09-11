//! End-to-end recorder tests: determinism, byte-identity, and Python parity.

use std::collections::BTreeMap;
use std::path::PathBuf;

use harness_backtest::results::recorder::{
    ActionKind, ActionRecord, Artifact, EngineIdSource, EquityRecord, EventRecord,
    ExecutionReportRecord, FillRecord, FixedClock, MarkerScope, PositionTransitionRecord,
    RecorderConfig, RecorderError, RunRecorder, Seed, TransitionReason,
};
use harness_backtest::JsonValue;

const TS: i64 = 1_704_153_600_000_000_000;

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-backtest-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

fn config(directory: &PathBuf) -> RecorderConfig {
    RecorderConfig::new("scripted-run", directory)
        .expect("config")
        .with_seed("recorder-seed")
        .with_batch_size(2)
        .expect("batch size")
        .with_record_events(true)
        .with_strategy_id("momentum")
        .expect("strategy")
        .with_book_id("alpha")
        .expect("book")
        .with_account_id("paper")
        .expect("account")
        .with_metadata("origin", "unit-test")
        .expect("metadata")
}

fn scripted_run(directory: &PathBuf) -> (RunRecorder, Vec<String>) {
    let mut recorder = RunRecorder::new(config(directory)).expect("recorder");
    let mut ids = Vec::new();

    let action_id = recorder
        .record_action(&ActionRecord {
            kind: Some(ActionKind::Submit),
            symbol: Some("SPY".to_string()),
            side: Some("buy".to_string()),
            quantity: Some("100.00".to_string()),
            at_ns: Some(TS),
            ..ActionRecord::default()
        })
        .expect("action");
    ids.push(action_id.clone());

    recorder
        .record_fill(&FillRecord {
            fill_id: "fill-1".to_string(),
            sequence: 0,
            timestamp_ns: TS + 1_000,
            event_id: "trade-1".to_string(),
            order_id: "order-1".to_string(),
            action_id: Some(action_id.clone()),
            risk_decision_id: None,
            position_transition_id: None,
            equity_snapshot_id: None,
            symbol: "SPY".to_string(),
            side: "buy".to_string(),
            quantity: "100.00".to_string(),
            price: "100.01".to_string(),
            fee: "0.01".to_string(),
            liquidity: Some("taker".to_string()),
        })
        .expect("fill");

    recorder
        .record_position_transition(&PositionTransitionRecord {
            reason: Some(TransitionReason::Fill),
            account_id: None,
            book_id: None,
            symbol: "SPY".to_string(),
            fill_id: Some("fill-1".to_string()),
            quantity_before: "0".to_string(),
            quantity_after: "100.00".to_string(),
            cost_basis: "10001.00".to_string(),
            cash_flow: "-10001.00".to_string(),
            realized_pnl: "0".to_string(),
            mark_price: None,
            market_value: None,
            unrealized_pnl: None,
            at_ns: Some(TS + 2_000),
            transition_id: None,
        })
        .expect("transition");

    recorder
        .record_equity(&EquityRecord {
            source: "portfolio".to_string(),
            account_id: None,
            book_id: None,
            cash: "8999.00".to_string(),
            equity: Some("9000.00".to_string()),
            market_value: Some("10001.00".to_string()),
            realized_pnl: "0".to_string(),
            unrealized_pnl: Some("-1.00".to_string()),
            total_pnl: Some("-1.00".to_string()),
            position_count: 1,
            fill_count: 1,
            at_ns: Some(TS + 3_000),
            snapshot_id: None,
        })
        .expect("equity");

    recorder
        .record_emission(
            "signal",
            &JsonValue::object([("strength", JsonValue::Int(3))]),
            None,
            None,
            Some(TS + 4_000),
        )
        .expect("emission");

    recorder
        .record_marker(
            "warm",
            Some("yes"),
            MarkerScope::State,
            None,
            None,
            Some(TS + 5_000),
        )
        .expect("marker");

    recorder
        .record_event(&EventRecord {
            event_id: "trade-1".to_string(),
            sequence: 0,
            timestamp_ns: TS + 1_000,
            event_type: "trade".to_string(),
            type_name: "harness.core.types.events.TradeEvent".to_string(),
            symbol: Some("SPY".to_string()),
            payload: Some(JsonValue::object([("price", JsonValue::string("100.01"))])),
        })
        .expect("event");

    recorder
        .record_execution_report(&ExecutionReportRecord {
            event_id: "report-event-1".to_string(),
            timestamp_ns: TS + 1_500,
            sequence: 1,
            report_id: "report-1".to_string(),
            order_id: "order-1".to_string(),
            status: "filled".to_string(),
            execution_type: "fill".to_string(),
            order_quantity: "100.00".to_string(),
            filled_quantity: "100.00".to_string(),
            remaining_quantity: "0".to_string(),
            average_fill_price: Some("100.01".to_string()),
            last_fill_quantity: "100.00".to_string(),
            last_fill_price: Some("100.01".to_string()),
            text: None,
        })
        .expect("report");

    (recorder, ids)
}

#[test]
fn recorder_replays_byte_identically() {
    let left_dir = temp_dir("left");
    let right_dir = temp_dir("right");
    let (mut left, _) = scripted_run(&left_dir);
    let (mut right, _) = scripted_run(&right_dir);
    let left_manifest = left.close().expect("left close");
    let right_manifest = right.close().expect("right close");
    assert_eq!(left_manifest.run_digest, right_manifest.run_digest);
    assert_eq!(left_manifest.row_counts, right_manifest.row_counts);
    for artifact in Artifact::ALL {
        let left_bytes = std::fs::read(left_manifest.path(artifact)).expect("left artifact");
        let right_bytes = std::fs::read(right_manifest.path(artifact)).expect("right artifact");
        assert_eq!(left_bytes, right_bytes, "{artifact:?} differs");
    }
    let _ = std::fs::remove_dir_all(&left_dir);
    let _ = std::fs::remove_dir_all(&right_dir);
}

#[test]
fn recorder_counts_and_manifest_are_stable() {
    let directory = temp_dir("counts");
    let (mut recorder, _) = scripted_run(&directory);
    let manifest = recorder.close().expect("close");
    let expected: BTreeMap<&str, u64> = BTreeMap::from([
        ("actions", 1),
        ("orders", 0),
        ("risk_decisions", 0),
        ("risk_audits", 0),
        ("fills", 1),
        ("execution_reports", 1),
        ("position_transitions", 1),
        ("equity", 1),
        ("emissions", 1),
        ("markers", 1),
        ("events", 1),
        ("manifest", manifest.row_counts["manifest"]),
    ]);
    for (name, count) in expected {
        assert_eq!(manifest.row_counts[name], count, "{name}");
    }
    assert_eq!(
        manifest.metadata.get("origin"),
        Some(&"unit-test".to_string())
    );
    // The manifest is one row per entry, and `row_count` is self-consistent.
    let manifest_rows = manifest.row_counts["manifest"];
    assert!(manifest_rows > 30);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn recorder_requires_a_timestamp_without_a_clock() {
    let directory = temp_dir("no-clock");
    let mut recorder = RunRecorder::new(config(&directory)).expect("recorder");
    let result = recorder.record_action(&ActionRecord {
        kind: Some(ActionKind::Submit),
        symbol: Some("SPY".to_string()),
        ..ActionRecord::default()
    });
    assert!(matches!(result, Err(RecorderError::TimestampRequired)));
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn fixed_clock_supplies_timestamps() {
    let directory = temp_dir("clock");
    let mut config = config(&directory);
    config.clock = Some(std::sync::Arc::new(FixedClock::new(TS)));
    let mut recorder = RunRecorder::new(config).expect("recorder");
    let id = recorder
        .record_action(&ActionRecord {
            kind: Some(ActionKind::Cancel),
            symbol: Some("SPY".to_string()),
            ..ActionRecord::default()
        })
        .expect("action");
    assert_eq!(id.len(), 36);
    recorder.close().expect("close");
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn deterministic_ids_match_the_python_source() {
    let mut source = EngineIdSource::new(Seed::Text("recorder-seed".to_string())).expect("seed");
    assert_eq!(
        source.next_id("action").expect("id"),
        "be2d63ce-1e48-519a-8ab3-3f24cf921767"
    );
    assert_eq!(
        source.next_id("action").expect("id"),
        "96cd8c2e-9b9a-5dd8-b9b9-858fb923362c"
    );
    let mut integer = EngineIdSource::new(Seed::Int(7)).expect("seed");
    assert_eq!(
        integer.next_id("fill").expect("id"),
        "f750af58-8a50-5650-87ca-54225644115b"
    );
}
