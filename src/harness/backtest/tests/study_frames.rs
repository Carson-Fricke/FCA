//! Atomic study assembly and dispatch, without changing tick replay.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::{
    assemble_study_frames, build_default_manifest, run_native_engine, BacktestOutcome,
    EngineBarFrame, EngineMarket, EngineOptions, EngineOrder, StrategyInstance, StrategyRuntime,
    StrategyStart,
};
use backtest::backtest::errors::BacktestError;
use backtest::backtest::fixture::{
    synthetic_fixture, ExchangeSession, MarketEvent, ReplayFingerprints, ReplayFixture,
    SyntheticDataOptions,
};
use backtest::backtest::job::{load_study_config, parse_study_document, parse_timestamp_ns};
use backtest::backtest::preflight::preflight_data;
use num::Decimal18;

const MINUTE: i64 = 60_000_000_000;

fn stamp(text: &str) -> i64 {
    parse_timestamp_ns(text).unwrap()
}

fn config(interval: &str) -> BacktestConfig {
    let mut config = BacktestConfig::new(
        "frames",
        stamp("2024-01-02T14:30:00Z"),
        stamp("2024-01-02T21:00:00Z"),
        7,
    )
    .unwrap();
    config.shell = "study".to_string();
    config.data_bar_interval = interval.to_string();
    config.symbols = vec!["SPY".to_string(), "AAPL".to_string(), "ABSENT".to_string()];
    config
}

fn bar(symbol: &str, timestamp_ns: i64) -> MarketEvent {
    MarketEvent::Bar {
        event_id: format!("{symbol}-{timestamp_ns}"),
        timestamp_ns,
        sequence: 0,
        symbol: symbol.to_string(),
        open: Decimal18::from_i64(10),
        high: Decimal18::from_i64(12),
        low: Decimal18::from_i64(9),
        close: Decimal18::from_i64(11),
        volume: Decimal18::from_i64(100),
    }
}

fn fixture(config: &BacktestConfig, events: Vec<MarketEvent>) -> ReplayFixture {
    ReplayFixture {
        sessions: vec![ExchangeSession {
            session_date_days: config.start_ns.div_euclid(86_400_000_000_000),
            open_ns: config.start_ns,
            close_ns: config.end_ns,
        }],
        fingerprints: ReplayFingerprints::of_events(&events),
        events,
        source_id: "test".to_string(),
        symbols: config.symbols.clone(),
        expected_data_fingerprint: None,
    }
}

#[derive(Default)]
struct Observer {
    frames: Arc<Mutex<Vec<EngineBarFrame>>>,
    ticks: Arc<Mutex<u64>>,
}

impl StrategyRuntime for Observer {
    fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }
    fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        *self.ticks.lock().unwrap() += 1;
        Ok(Vec::new())
    }
    fn on_bar_frame(&mut self, frame: &EngineBarFrame) -> Result<(), BacktestError> {
        self.frames.lock().unwrap().push(frame.clone());
        Ok(())
    }
    fn published_metrics(&self) -> BTreeMap<String, String> {
        BTreeMap::new()
    }
}

fn run(config: &BacktestConfig, fixture: &ReplayFixture, observer: Observer) -> BacktestOutcome {
    let preflight = preflight_data(config, fixture).unwrap();
    let manifest = build_default_manifest(config, fixture, &preflight).unwrap();
    run_native_engine(
        config,
        fixture,
        &preflight,
        manifest,
        &EngineOptions::default(),
        vec![StrategyInstance {
            instance_id: "observer".to_string(),
            runtime: Box::new(observer),
        }],
        &SyntheticDataOptions::default(),
    )
    .unwrap()
}

#[test]
fn groups_timestamp_union_in_symbol_order_without_carry_or_all_missing_frames() {
    let config = config("1min");
    let start = config.start_ns;
    let fixture = fixture(
        &config,
        vec![
            bar("SPY", start + 2 * MINUTE),
            bar("SPY", start),
            bar("AAPL", start),
        ],
    );
    let frames =
        assemble_study_frames(&config, &fixture, &SyntheticDataOptions::default()).unwrap();
    assert_eq!(frames.len(), 2);
    assert_eq!(
        frames[0]
            .bars_by_symbol
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["AAPL", "SPY"]
    );
    assert_eq!(frames[0].missing_symbols, ["ABSENT"]);
    assert_eq!(frames[1].missing_symbols, ["AAPL", "ABSENT"]);
    assert_eq!(frames[1].interval_start_ns, start + 2 * MINUTE);
    assert_eq!(frames[1].available_at_ns, start + 3 * MINUTE);
    assert!(preflight_data(&config, &fixture).is_ok());
}

#[test]
fn dispatches_once_at_availability_with_raw_counts_and_order_independent_hash() {
    let mut config = config("1min");
    config.end_ns = config.start_ns + 2 * MINUTE + MINUTE / 2;
    let events = vec![
        bar("SPY", config.start_ns),
        bar("AAPL", config.start_ns),
        bar("SPY", config.start_ns + MINUTE),
        bar("SPY", config.start_ns + 2 * MINUTE),
    ];
    let observer = Observer::default();
    let seen = observer.frames.clone();
    let ticks = observer.ticks.clone();
    let first = run(&config, &fixture(&config, events.clone()), observer);
    assert_eq!(first.market_event_count, 4);
    assert_eq!(first.preflight.bar_count, 4);
    assert_eq!(first.bar_frame_count, 2);
    assert_eq!(first.dispatched_count, 2);
    assert_eq!(first.order_count, 0);
    assert!(first.fills.is_empty());
    assert_eq!(*ticks.lock().unwrap(), 0);
    assert_eq!(
        seen.lock().unwrap()[0].available_at_ns,
        config.start_ns + MINUTE
    );
    let second = run(
        &config,
        &fixture(&config, events.into_iter().rev().collect()),
        Observer::default(),
    );
    assert_eq!(first.dispatch_hash, second.dispatch_hash);
    config.end_ns = config.start_ns + 2 * MINUTE;
    let boundary = run(
        &config,
        &fixture(&config, vec![bar("SPY", config.start_ns + MINUTE)]),
        Observer::default(),
    );
    assert_eq!(
        boundary.bar_frame_count, 1,
        "availability equal to end is included"
    );
}

#[test]
fn rejects_duplicates_unexpected_symbols_ticks_and_misaligned_starts() {
    let config = config("1min");
    for events in [
        vec![bar("SPY", config.start_ns), bar("SPY", config.start_ns)],
        vec![bar("OTHER", config.start_ns)],
        vec![bar("SPY", config.start_ns + 1)],
        vec![bar("SPY", config.end_ns)],
    ] {
        assert!(assemble_study_frames(
            &config,
            &fixture(&config, events),
            &SyntheticDataOptions::default()
        )
        .is_err());
    }
    let ticks = synthetic_fixture(&config, &SyntheticDataOptions::default()).unwrap();
    assert!(assemble_study_frames(&config, &ticks, &SyntheticDataOptions::default()).is_err());
    assert!(preflight_data(&config, &fixture(&config, Vec::new())).is_err());
}

#[test]
fn daily_uses_real_session_close_dst_and_early_close_not_window_clipping() {
    for (open, close) in [
        ("2024-01-02T14:30:00Z", "2024-01-02T21:00:00Z"),
        ("2024-07-03T13:30:00Z", "2024-07-03T17:00:00Z"),
        ("2024-11-29T14:30:00Z", "2024-11-29T18:00:00Z"),
    ] {
        let mut config = config("1d");
        config.start_ns = stamp(open);
        config.end_ns = stamp(close);
        let fixture = fixture(&config, vec![bar("SPY", config.start_ns)]);
        let frames =
            assemble_study_frames(&config, &fixture, &SyntheticDataOptions::default()).unwrap();
        assert_eq!(frames[0].interval_end_ns, stamp(close));
        config.end_ns -= 1;
        assert!(
            assemble_study_frames(&config, &fixture, &SyntheticDataOptions::default())
                .unwrap()
                .is_empty()
        );
        config.end_ns = stamp(close);
        config.data_bar_interval = "1min".to_string();
        let last = fixture_with_last_minute(&config);
        assert_eq!(
            assemble_study_frames(&config, &last, &SyntheticDataOptions::default()).unwrap()[0]
                .available_at_ns,
            stamp(close)
        );
    }
}

fn fixture_with_last_minute(config: &BacktestConfig) -> ReplayFixture {
    fixture(config, vec![bar("SPY", config.end_ns - MINUTE)])
}

#[test]
fn crypto_daily_is_utc_and_mixed_daily_is_explicitly_rejected() {
    let mut config = config("1d");
    config.start_ns = stamp("2024-07-06T00:00:00Z");
    config.end_ns = stamp("2024-07-07T00:00:00Z");
    config.symbols = vec!["BTC-USD".to_string(), "ETH-USD".to_string()];
    let fixture = fixture(&config, vec![bar("BTC-USD", config.start_ns)]);
    let frames =
        assemble_study_frames(&config, &fixture, &SyntheticDataOptions::default()).unwrap();
    assert_eq!(frames[0].interval_end_ns, config.end_ns);
    assert_eq!(frames[0].missing_symbols, ["ETH-USD"]);
    config.symbols.push("SPY".to_string());
    let error =
        assemble_study_frames(&config, &fixture, &SyntheticDataOptions::default()).unwrap_err();
    assert!(error.to_string().contains("mixed equity/crypto DAILY"));
}

#[test]
fn tick_callbacks_counts_and_repeatability_are_unchanged() {
    let mut config = config("1min");
    config.shell = "backtest".to_string();
    config.symbols = vec!["SPY".to_string()];
    let fixture = synthetic_fixture(&config, &SyntheticDataOptions::default()).unwrap();
    let observer = Observer::default();
    let seen = observer.frames.clone();
    let ticks = observer.ticks.clone();
    let first = run(&config, &fixture, observer);
    let second = run(&config, &fixture, Observer::default());
    assert_eq!(*ticks.lock().unwrap(), fixture.events.len() as u64);
    assert!(seen.lock().unwrap().is_empty());
    assert_eq!(first.bar_frame_count, 0);
    assert_eq!(first.market_event_count, fixture.events.len() as u64);
    assert_eq!(first.dispatched_count, first.market_event_count);
    assert_eq!(first.artifact_json(), second.artifact_json());
    assert!(!first.artifact_json().contains("bar_frames"));
}

#[test]
fn study_document_and_file_require_explicit_canonical_interval() {
    let path = std::env::temp_dir().join(format!(
        "backtest-study-interval-{}.json",
        std::process::id()
    ));
    for interval in [
        serde_json::Value::Null,
        serde_json::json!(1),
        serde_json::json!("DAILY"),
        serde_json::json!("1m"),
        serde_json::json!("1min"),
        serde_json::json!("1d"),
    ] {
        let mut document = serde_json::json!({"shell": "study", "run_id": "test", "data": {}});
        if !interval.is_null() {
            document["data"]["bar_interval"] = interval.clone();
        }
        let expected = interval == "1min" || interval == "1d";
        assert_eq!(parse_study_document(&document).is_ok(), expected);
        std::fs::write(&path, document.to_string()).unwrap();
        assert_eq!(load_study_config(&path).is_ok(), expected);
    }
    std::fs::remove_file(path).unwrap();
}

#[test]
fn non_study_runtime_rejects_frames_and_replay_verification_stays_raw() {
    struct TickOnly;
    impl StrategyRuntime for TickOnly {
        fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
            Ok(())
        }
        fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
            panic!("study must not call the tick callback")
        }
    }
    let config = config("1min");
    let fixture = fixture(&config, vec![bar("SPY", config.start_ns)]);
    let mut preflight = preflight_data(&config, &fixture).unwrap();
    let manifest = build_default_manifest(&config, &fixture, &preflight).unwrap();
    let rejected = run_native_engine(
        &config,
        &fixture,
        &preflight,
        manifest.clone(),
        &EngineOptions::default(),
        vec![StrategyInstance {
            instance_id: "tick-only".to_string(),
            runtime: Box::new(TickOnly),
        }],
        &SyntheticDataOptions::default(),
    );
    assert!(matches!(rejected, Err(BacktestError::StrategyExecution(_))));
    preflight.event_count += 1;
    let mismatch = run_native_engine(
        &config,
        &fixture,
        &preflight,
        manifest,
        &EngineOptions::default(),
        Vec::new(),
        &SyntheticDataOptions::default(),
    );
    assert!(matches!(mismatch, Err(BacktestError::DataPreflight(_))));
}

#[test]
fn dispatch_hash_covers_missing_symbols_bounds_and_bar_values() {
    let config = config("1min");
    let first = run(
        &config,
        &fixture(&config, vec![bar("SPY", config.start_ns)]),
        Observer::default(),
    );
    let mut changed = bar("SPY", config.start_ns);
    if let MarketEvent::Bar { close, .. } = &mut changed {
        *close = Decimal18::from_i64(12);
    }
    let second = run(
        &config,
        &fixture(&config, vec![changed]),
        Observer::default(),
    );
    assert_ne!(first.dispatch_hash, second.dispatch_hash);
    let mut smaller = config.clone();
    smaller.symbols.retain(|symbol| symbol != "ABSENT");
    let third = run(
        &smaller,
        &fixture(&smaller, vec![bar("SPY", smaller.start_ns)]),
        Observer::default(),
    );
    assert_ne!(first.dispatch_hash, third.dispatch_hash);
}
