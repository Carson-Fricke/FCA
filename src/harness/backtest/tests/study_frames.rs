//! Atomic study assembly and dispatch, without changing tick replay.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use backtest::backtest::config::{BacktestConfig, StrategyInstanceConfig};
use backtest::backtest::engine_core::{
    assemble_study_frames, build_default_manifest, run_native_engine, BacktestOutcome,
    EngineBarFrame, EngineMarket, EngineOptions, EngineOrder, MarketSubscription, StrategyInstance,
    StrategyRuntime, StrategyStart,
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

fn bars_subscription(symbols: &[String], interval: &str) -> MarketSubscription {
    MarketSubscription {
        data_type: "bars".to_string(),
        symbols: symbols.to_vec(),
        interval: Some(interval.to_string()),
    }
}

fn sync_study_subscriptions(config: &mut BacktestConfig) {
    config.data_subscriptions = vec![bars_subscription(
        &config.symbols,
        &config.data_bar_interval,
    )];
    config.data_subscriptions_declared = true;
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
    config.data_source_bar_interval = interval.to_string();
    config.symbols = vec!["SPY".to_string(), "AAPL".to_string(), "ABSENT".to_string()];
    sync_study_subscriptions(&mut config);
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
    outputs: BTreeMap<String, serde_json::Value>,
    ticks: Arc<Mutex<u64>>,
    subscriptions: Vec<MarketSubscription>,
}

impl StrategyRuntime for Observer {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(self.subscriptions.clone())
    }
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
    fn published_outputs(&self) -> BTreeMap<String, serde_json::Value> {
        self.outputs.clone()
    }
}

fn run(
    config: &BacktestConfig,
    fixture: &ReplayFixture,
    mut observer: Observer,
) -> BacktestOutcome {
    if observer.subscriptions.is_empty() {
        observer.subscriptions = config.data_subscriptions.clone();
    }
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
    config.symbols = vec!["SPY".to_string(), "AAPL".to_string()];
    sync_study_subscriptions(&mut config);
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
    let mut boundary_config = config.clone();
    boundary_config.symbols = vec!["SPY".to_string()];
    sync_study_subscriptions(&mut boundary_config);
    boundary_config.end_ns = boundary_config.start_ns + 2 * MINUTE;
    let boundary = run(
        &boundary_config,
        &fixture(
            &boundary_config,
            vec![bar("SPY", boundary_config.start_ns + MINUTE)],
        ),
        Observer::default(),
    );
    assert_eq!(
        boundary.bar_frame_count, 1,
        "availability equal to end is included"
    );
}

#[test]
fn rejects_duplicates_unexpected_symbols_and_misaligned_starts_but_ignores_ticks() {
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
    assert!(
        assemble_study_frames(&config, &ticks, &SyntheticDataOptions::default())
            .unwrap()
            .is_empty()
    );
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
        config.data_source_bar_interval = "1min".to_string();
        sync_study_subscriptions(&mut config);
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
    sync_study_subscriptions(&mut config);
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
    config.data_subscriptions = vec![
        subscription("quote", &["SPY"], None),
        subscription("trade", &["SPY"], None),
    ];
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
fn study_documents_require_explicit_subscriptions() {
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
        let mut document =
            serde_json::json!({"shell": "study", "job_id": "test", "data": {"symbols": ["SPY"]}});
        if !interval.is_null() {
            document["data"]["bar_interval"] = interval.clone();
        }
        assert!(parse_study_document(&document).is_err());
        std::fs::write(&path, document.to_string()).unwrap();
        assert!(load_study_config(&path).is_err());
    }
    let valid = serde_json::json!({"shell": "study", "job_id": "test",
        "data": {"symbols": ["SPY"],
            "subscriptions": [{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}]}});
    assert!(parse_study_document(&valid).is_ok());
    std::fs::write(&path, valid.to_string()).unwrap();
    assert!(load_study_config(&path).is_ok());
    std::fs::remove_file(path).unwrap();
}

#[test]
fn non_study_runtime_rejects_frames_and_replay_verification_stays_raw() {
    struct TickOnly;
    impl StrategyRuntime for TickOnly {
        fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
            Some(vec![subscription("bars", &["SPY"], Some("1min"))])
        }
        fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
            Ok(())
        }
        fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
            panic!("study must not call the tick callback")
        }
    }
    let mut config = config("1min");
    config.symbols = vec!["SPY".to_string()];
    sync_study_subscriptions(&mut config);
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
    config.data_subscriptions.clear();
    config.data_subscriptions_declared = false;
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
fn dispatch_hash_covers_bounds_and_bar_values() {
    let mut config = config("1min");
    config.symbols = vec!["SPY".to_string()];
    sync_study_subscriptions(&mut config);
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
    let third = run(
        &config,
        &fixture(
            &config,
            vec![
                bar("SPY", config.start_ns),
                bar("SPY", config.start_ns + MINUTE),
            ],
        ),
        Observer::default(),
    );
    assert_ne!(first.dispatch_hash, third.dispatch_hash);
}

fn subscription(kind: &str, symbols: &[&str], interval: Option<&str>) -> MarketSubscription {
    MarketSubscription {
        data_type: kind.into(),
        symbols: symbols.iter().map(|s| (*s).into()).collect(),
        interval: interval.map(str::to_string),
    }
}

struct RoutedObserver {
    inputs: Vec<MarketSubscription>,
    seen: Arc<Mutex<Vec<String>>>,
}

impl StrategyRuntime for RoutedObserver {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(self.inputs.clone())
    }
    fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }
    fn on_market(&mut self, event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        let kind = match event {
            EngineMarket::Quote { .. } => "quote",
            EngineMarket::Trade { .. } => "trade",
            _ => panic!("raw bar"),
        };
        self.seen.lock().unwrap().push(format!(
            "{kind}:{}:{}",
            event.symbol(),
            event.timestamp_ns()
        ));
        Ok(Vec::new())
    }
    fn on_bar_frame(&mut self, frame: &EngineBarFrame) -> Result<(), BacktestError> {
        self.seen.lock().unwrap().push(format!(
            "{}:{}:{}",
            frame.interval,
            frame
                .bars_by_symbol
                .keys()
                .cloned()
                .collect::<Vec<_>>()
                .join(","),
            frame.available_at_ns
        ));
        Ok(())
    }
}

fn quote(symbol: &str, timestamp_ns: i64) -> MarketEvent {
    MarketEvent::Quote {
        event_id: format!("q-{symbol}-{timestamp_ns}"),
        timestamp_ns,
        sequence: 1,
        symbol: symbol.into(),
        bid: Decimal18::from_i64(10),
        ask: Decimal18::from_i64(11),
        bid_quantity: Decimal18::from_i64(100),
        ask_quantity: Decimal18::from_i64(100),
        venue: "test".into(),
    }
}

#[test]
fn mixed_replay_routes_each_consumer_and_never_dispatches_future_frames_ahead_of_ticks() {
    for shell in ["study", "backtest"] {
        let mut config = config("1min");
        config.shell = shell.into();
        config.symbols = vec!["SPY".into(), "AAPL".into()];
        config.data_subscriptions = vec![
            subscription("quote", &["SPY", "AAPL"], None),
            subscription("bars", &["SPY", "AAPL"], Some("1min")),
        ];
        config.strategy_instances = ["mixed", "other"]
            .into_iter()
            .map(|instance_id| StrategyInstanceConfig {
                instance_id: instance_id.into(),
                implementation: "observer".into(),
                bundle_hash: "observer".into(),
                parameters: BTreeMap::new(),
                symbols: vec!["SPY".into(), "AAPL".into()],
                capital_fraction: "0.5".into(),
            })
            .collect();
        let start = config.start_ns;
        let fixture = fixture(
            &config,
            vec![
                bar("SPY", start),
                bar("AAPL", start),
                quote("SPY", start + MINUTE / 2),
                quote("AAPL", start + MINUTE / 3),
                quote("SPY", start + MINUTE + 1),
            ],
        );
        let mixed = Arc::new(Mutex::new(Vec::new()));
        let other = Arc::new(Mutex::new(Vec::new()));
        let strategies = vec![
            StrategyInstance {
                instance_id: "mixed".into(),
                runtime: Box::new(RoutedObserver {
                    inputs: vec![
                        subscription("quote", &["SPY"], None),
                        subscription("bars", &["SPY"], Some("1min")),
                    ],
                    seen: mixed.clone(),
                }),
            },
            StrategyInstance {
                instance_id: "other".into(),
                runtime: Box::new(RoutedObserver {
                    inputs: vec![
                        subscription("quote", &["AAPL"], None),
                        subscription("bars", &["AAPL"], Some("1min")),
                    ],
                    seen: other.clone(),
                }),
            },
        ];
        let preflight = preflight_data(&config, &fixture).unwrap();
        let manifest = build_default_manifest(&config, &fixture, &preflight).unwrap();
        let outcome = run_native_engine(
            &config,
            &fixture,
            &preflight,
            manifest,
            &EngineOptions::default(),
            strategies,
            &SyntheticDataOptions::default(),
        )
        .unwrap();
        assert_eq!(
            *mixed.lock().unwrap(),
            [
                format!("quote:SPY:{}", start + MINUTE / 2),
                format!("1min:SPY:{}", start + MINUTE),
                format!("quote:SPY:{}", start + MINUTE + 1)
            ]
        );
        assert_eq!(
            *other.lock().unwrap(),
            [
                format!("quote:AAPL:{}", start + MINUTE / 3),
                format!("1min:AAPL:{}", start + MINUTE)
            ]
        );
        assert_eq!(outcome.bar_frame_count, 1);
        assert!(!outcome.manifest.effective_subscriptions.is_empty());
        assert_eq!(outcome.order_count, 0);
    }
}

#[test]
fn tick_only_study_parses_without_bar_interval_and_rejects_invalid_echoes() {
    let document = serde_json::json!({"shell":"study", "job_id":"ticks", "data":{"symbols":["SPY"],"subscriptions":[{"data_type":"trade", "symbols":["SPY"]}]}});
    let parsed = parse_study_document(&document).unwrap();
    assert_eq!(
        parsed.data_subscriptions,
        [subscription("trade", &["SPY"], None)]
    );
    for input in [
        serde_json::json!({"data_type":"trade","symbols":[]}),
        serde_json::json!({"data_type":"trade","symbols":["SPY"],"interval":"1min"}),
        serde_json::json!({"data_type":"bars","symbols":["SPY"]}),
        serde_json::json!({"data_type":"bars","symbols":["SPY"],"interval":"1m"}),
        serde_json::json!({"data_type":"quote","symbols":["SPY"],"extra":1}),
    ] {
        let mut invalid = document.clone();
        invalid["data"]["subscriptions"] = serde_json::json!([input]);
        assert!(parse_study_document(&invalid).is_err());
    }
    let mut bars = document;
    bars["data"]["subscriptions"] =
        serde_json::json!([{"data_type":"bars","symbols":["SPY"],"interval":"1d"}]);
    assert!(parse_study_document(&bars).is_ok());
}

#[test]
fn requested_type_cannot_be_satisfied_by_other_stream_and_echo_is_exact() {
    let mut config = config("1min");
    config.symbols = vec!["SPY".into()];
    let fixture = fixture(&config, vec![quote("SPY", config.start_ns)]);
    let input = subscription("trade", &["SPY"], None);
    assert!(
        backtest::backtest::preflight::validate_subscription_coverage(
            &fixture,
            std::slice::from_ref(&input)
        )
        .is_err()
    );
    config.data_subscriptions = vec![input];
    let strategies = vec![StrategyInstance {
        instance_id: "quotes".into(),
        runtime: Box::new(RoutedObserver {
            inputs: vec![subscription("quote", &["SPY"], None)],
            seen: Arc::default(),
        }),
    }];
    assert!(backtest::backtest::engine_core::runtime_subscriptions(&config, &strategies).is_err());
    assert!(backtest::backtest::engine_core::normalize_subscriptions(&[
        subscription("bars", &["SPY"], Some("1min")),
        subscription("bars", &["SPY"], Some("1d"))
    ])
    .is_ok());
}

#[test]
fn frame_orders_execute_only_in_backtests_and_never_fill_from_bars() {
    struct FrameBuyer;
    impl StrategyRuntime for FrameBuyer {
        fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
            Some(vec![subscription("bars", &["SPY"], Some("1min"))])
        }
        fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
            Ok(())
        }
        fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
            panic!("execution feeds must not reach a bars consumer")
        }
        fn on_frame_orders(
            &mut self,
            frame: &EngineBarFrame,
        ) -> Result<Vec<EngineOrder>, BacktestError> {
            Ok(vec![EngineOrder {
                order_id: "buy".into(),
                symbol: "SPY".into(),
                side: "buy".into(),
                quantity: num::Decimal::from_i128(1),
                order_type: "market".into(),
                time_in_force: "day".into(),
                limit_price: None,
                stop_price: None,
                timestamp_ns: frame.available_at_ns,
                sequence: 0,
                event_id: "buy-event".into(),
            }])
        }
    }
    for (shell, ticks, succeeds, fills) in [
        ("study", false, false, 0),
        ("backtest", false, true, 0),
        ("backtest", true, true, 1),
    ] {
        let mut config = config("1min");
        config.shell = shell.into();
        config.symbols = vec!["SPY".into()];
        sync_study_subscriptions(&mut config);
        let mut events = vec![bar("SPY", config.start_ns)];
        if ticks {
            events.push(quote("SPY", config.start_ns + MINUTE + 1));
        }
        let fixture = fixture(&config, events);
        let preflight = preflight_data(&config, &fixture).unwrap();
        let manifest = build_default_manifest(&config, &fixture, &preflight).unwrap();
        let outcome = run_native_engine(
            &config,
            &fixture,
            &preflight,
            manifest,
            &EngineOptions::default(),
            vec![StrategyInstance {
                instance_id: "buyer".into(),
                runtime: Box::new(FrameBuyer),
            }],
            &SyntheticDataOptions::default(),
        );
        assert_eq!(outcome.is_ok(), succeeds);
        if succeeds {
            let outcome = outcome.unwrap();
            assert_eq!(outcome.fills.len(), fills);
            assert_eq!(outcome.manifest.execution_subscriptions.len(), 2);
        }
    }
}

#[test]
fn subscription_serde_contract_is_strict_and_round_trips() {
    let input = subscription("quote", &["SPY"], None);
    let text = serde_json::to_string(&input).unwrap();
    assert_eq!(
        serde_json::from_str::<MarketSubscription>(&text).unwrap(),
        input
    );
    assert!(serde_json::from_str::<MarketSubscription>(
        r#"{"data_type":"quote","symbols":["SPY"],"unknown":true}"#
    )
    .is_err());
}

fn buy(id: &str, timestamp_ns: i64) -> EngineOrder {
    EngineOrder {
        order_id: id.into(),
        symbol: "SPY".into(),
        side: "buy".into(),
        quantity: num::Decimal::from_i128(1),
        order_type: "market".into(),
        time_in_force: "day".into(),
        limit_price: None,
        stop_price: None,
        timestamp_ns,
        sequence: 0,
        event_id: id.into(),
    }
}

#[test]
fn same_time_ticks_precede_frames_and_generated_fills_use_current_quotes() {
    struct Buyer {
        seen: Arc<Mutex<Vec<String>>>,
        availability: i64,
    }
    impl StrategyRuntime for Buyer {
        fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
            Some(vec![
                subscription("quote", &["SPY"], None),
                subscription("bars", &["SPY"], Some("1min")),
            ])
        }
        fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
            Ok(())
        }
        fn on_market(&mut self, event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
            self.seen
                .lock()
                .unwrap()
                .push(format!("quote:{}", event.sequence()));
            Ok(
                if event.timestamp_ns() == self.availability && event.sequence() == 1 {
                    vec![buy("tick-buy", self.availability)]
                } else {
                    Vec::new()
                },
            )
        }
        fn on_frame_orders(
            &mut self,
            frame: &EngineBarFrame,
        ) -> Result<Vec<EngineOrder>, BacktestError> {
            self.seen.lock().unwrap().push("frame".into());
            Ok(vec![buy("frame-buy", frame.available_at_ns)])
        }
        fn on_fill(
            &mut self,
            fill: &backtest::backtest::engine_core::EngineFill,
        ) -> Result<(), BacktestError> {
            self.seen
                .lock()
                .unwrap()
                .push(format!("fill:{}", fill.order_id));
            Ok(())
        }
    }
    let mut config = config("1min");
    config.shell = "backtest".into();
    config.symbols = vec!["SPY".into()];
    config.data_subscriptions = vec![
        subscription("quote", &["SPY"], None),
        subscription("bars", &["SPY"], Some("1min")),
    ];
    let availability = config.start_ns + MINUTE;
    let mut first = quote("SPY", availability);
    let mut last = quote("SPY", availability);
    if let MarketEvent::Quote { ask, .. } = &mut first {
        *ask = Decimal18::from_i64(21);
    }
    if let MarketEvent::Quote {
        sequence,
        ask,
        event_id,
        ..
    } = &mut last
    {
        *sequence = 100;
        *ask = Decimal18::from_i64(31);
        *event_id = "last-quote".into();
    }
    // Deliberately unordered raw inputs, with a cached pre-availability quote.
    let fixture = fixture(
        &config,
        vec![
            last,
            bar("SPY", config.start_ns),
            first,
            quote("SPY", config.start_ns),
        ],
    );
    let preflight = preflight_data(&config, &fixture).unwrap();
    let manifest = build_default_manifest(&config, &fixture, &preflight).unwrap();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let outcome = run_native_engine(
        &config,
        &fixture,
        &preflight,
        manifest,
        &EngineOptions::default(),
        vec![StrategyInstance {
            instance_id: "buyer".into(),
            runtime: Box::new(Buyer {
                seen: seen.clone(),
                availability,
            }),
        }],
        &SyntheticDataOptions::default(),
    )
    .unwrap();
    assert_eq!(
        *seen.lock().unwrap(),
        [
            "quote:1",
            "quote:1",
            "quote:100",
            "frame",
            "fill:tick-buy",
            "fill:frame-buy"
        ]
    );
    assert_eq!(
        outcome
            .fills
            .iter()
            .map(|fill| fill.price)
            .collect::<Vec<_>>(),
        [num::Decimal::from_i128(21), num::Decimal::from_i128(31)]
    );
    assert_eq!(outcome.manifest.execution_subscriptions.len(), 2);
    assert_eq!(outcome.manifest.acquired_subscriptions.len(), 3);
}

#[test]
fn stop_rejection_and_duplicate_metrics_fail_after_all_runtimes_stop() {
    struct Stopper {
        reject: bool,
        stops: Arc<Mutex<u64>>,
    }
    impl StrategyRuntime for Stopper {
        fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
            Some(vec![subscription("quote", &["SPY"], None)])
        }
        fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
            Ok(())
        }
        fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
            Ok(Vec::new())
        }
        fn on_stop(&mut self) -> Result<(), BacktestError> {
            *self.stops.lock().unwrap() += 1;
            if self.reject {
                Err(BacktestError::StrategyExecution(
                    "stop metric rejected".into(),
                ))
            } else {
                Ok(())
            }
        }
        fn published_metrics(&self) -> BTreeMap<String, String> {
            if self.reject {
                BTreeMap::new()
            } else {
                BTreeMap::from([("shared".into(), "1".into())])
            }
        }
    }
    for reject in [true, false] {
        let mut config = config("1min");
        config.symbols = vec!["SPY".into()];
        config.data_subscriptions = vec![subscription("quote", &["SPY"], None)];
        let fixture = fixture(&config, vec![quote("SPY", config.start_ns)]);
        let preflight = preflight_data(&config, &fixture).unwrap();
        let manifest = build_default_manifest(&config, &fixture, &preflight).unwrap();
        let stops = Arc::new(Mutex::new(0));
        let strategies = (0..2)
            .map(|i| StrategyInstance {
                instance_id: format!("stopper-{i}"),
                runtime: Box::new(Stopper {
                    reject: reject && i == 0,
                    stops: stops.clone(),
                }) as Box<dyn StrategyRuntime>,
            })
            .collect();
        let error = run_native_engine(
            &config,
            &fixture,
            &preflight,
            manifest,
            &EngineOptions::default(),
            strategies,
            &SyntheticDataOptions::default(),
        )
        .unwrap_err();
        assert!(matches!(error, BacktestError::StrategyExecution(_)));
        assert!(error.to_string().contains(if reject {
            "stop metric rejected"
        } else {
            "published by both"
        }));
        assert_eq!(*stops.lock().unwrap(), 2);
    }
}

#[test]
fn explicit_bars_need_completed_frames_for_each_symbol_before_start() {
    let mut config = config("1min");
    config.symbols = vec!["SPY".into(), "AAPL".into()];
    sync_study_subscriptions(&mut config);
    config.end_ns = config.start_ns + MINUTE + MINUTE / 2;
    let fixture = fixture(
        &config,
        vec![
            bar("SPY", config.start_ns),
            bar("AAPL", config.start_ns + MINUTE),
        ],
    );
    let preflight = preflight_data(&config, &fixture).unwrap();
    let manifest = build_default_manifest(&config, &fixture, &preflight).unwrap();
    let strategies = vec![StrategyInstance {
        instance_id: "observer".into(),
        runtime: Box::new(RoutedObserver {
            inputs: vec![subscription("bars", &["SPY", "AAPL"], Some("1min"))],
            seen: Arc::default(),
        }),
    }];
    let error = run_native_engine(
        &config,
        &fixture,
        &preflight,
        manifest,
        &EngineOptions::default(),
        strategies,
        &SyntheticDataOptions::default(),
    )
    .unwrap_err();
    assert!(error
        .to_string()
        .contains("bars/AAPL completed frame coverage"));
}

#[test]
fn rfc3339_bounds_preserve_nanoseconds_offsets_and_reject_provided_invalid_values() {
    let base = stamp("2024-01-02T14:30:00Z");
    for text in [
        "2024-01-02T14:30:00.123456789Z",
        "2024-01-02T16:30:00.123456789+02:00",
        "2024-01-02T09:00:00.123456789-05:30",
    ] {
        assert_eq!(parse_timestamp_ns(text), Some(base + 123_456_789));
    }
    assert_eq!(
        parse_timestamp_ns("1969-12-31T23:59:59.999999999Z"),
        Some(-1)
    );
    let document = serde_json::json!({"shell":"study", "job_id":"bounds", "start":"2024-01-02T16:30:00.123456789+02:00", "end":"2024-01-02T14:30:00.123456790Z", "data":{"symbols":["SPY"],"subscriptions":[{"data_type":"quote","symbols":["SPY"]}]}});
    let parsed = parse_study_document(&document).unwrap();
    assert_eq!(
        (parsed.start_ns, parsed.end_ns),
        (base + 123_456_789, base + 123_456_790)
    );
    for invalid in [
        "2024-02-30T00:00:00Z",
        "2024-01-02T24:00:00Z",
        "2024-01-02T00:60:00Z",
        "2024-01-02T00:00:60Z",
        "2024-01-02T00:00:00+24:00",
        "2024-01-02T00:00:00+00:60",
        "2024-01-02T00:00:00+0a:00",
        "2024-01-02T00:00:00.1aZ",
        "2024-01-02T00:00:00.Z",
        "2024-01-02T00:00:00.1234567891Z",
        "2024-01-02T00:00:00",
        "9999-01-02T00:00:00Z",
    ] {
        assert_eq!(parse_timestamp_ns(invalid), None, "{invalid}");
        for key in ["start", "end"] {
            let mut bad = document.clone();
            bad[key] = serde_json::json!(invalid);
            assert!(parse_study_document(&bad)
                .unwrap_err()
                .to_string()
                .contains(key));
        }
    }
    for key in ["start", "end"] {
        for value in [serde_json::Value::Null, serde_json::json!(123)] {
            let mut bad = document.clone();
            bad[key] = value;
            assert!(parse_study_document(&bad).is_err());
        }
    }
    assert!(BacktestConfig::from_yaml("shell: backtest\njob_id: defaults\n").is_err());
}

#[test]
fn runtime_requirements_must_match_declared_subscriptions() {
    struct Undeclared;
    impl StrategyRuntime for Undeclared {
        fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
            Ok(())
        }
        fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
            Ok(Vec::new())
        }
        fn on_bar_frame(&mut self, _: &EngineBarFrame) -> Result<(), BacktestError> {
            Ok(())
        }
        fn published_metrics(&self) -> BTreeMap<String, String> {
            BTreeMap::new()
        }
    }
    let mut config = config("1min");
    config.symbols = vec!["SPY".into()];
    sync_study_subscriptions(&mut config);
    let declared = config.data_subscriptions.clone();
    let undeclared = vec![StrategyInstance {
        instance_id: "undeclared".into(),
        runtime: Box::new(Undeclared),
    }];
    assert!(backtest::backtest::engine_core::runtime_subscriptions(&config, &undeclared).is_err());
    let matching = vec![StrategyInstance {
        instance_id: "study".into(),
        runtime: Box::new(RoutedObserver {
            inputs: declared.clone(),
            seen: Arc::default(),
        }),
    }];
    assert!(backtest::backtest::engine_core::runtime_subscriptions(&config, &matching).is_ok());
    for inputs in [
        vec![subscription("bars", &["AAPL"], Some("1min"))],
        vec![subscription("bars", &["SPY"], Some("1d"))],
        vec![subscription("quote", &["SPY"], None)],
    ] {
        let strategies = vec![StrategyInstance {
            instance_id: "study".into(),
            runtime: Box::new(RoutedObserver {
                inputs,
                seen: Arc::default(),
            }),
        }];
        assert!(
            backtest::backtest::engine_core::runtime_subscriptions(&config, &strategies).is_err()
        );
    }
}

#[test]
fn multi_timeframe_replay_aggregates_completed_windows_from_one_source() {
    let mut config = config("1min");
    config.symbols = vec!["SPY".into()];
    config.end_ns = config.start_ns + 16 * MINUTE;
    config.data_subscriptions = vec![
        bars_subscription(&config.symbols, "1min"),
        bars_subscription(&config.symbols, "15min"),
    ];
    let events = (0..16)
        .map(|minute| bar("SPY", config.start_ns + minute * MINUTE))
        .collect();
    let observer = Observer::default();
    let seen = observer.frames.clone();
    let outcome = run(&config, &fixture(&config, events), observer);
    let frames = seen.lock().unwrap();
    assert_eq!(outcome.bar_frame_count, 17);
    let slow: Vec<_> = frames
        .iter()
        .filter(|frame| frame.interval == "15min")
        .collect();
    assert_eq!(slow.len(), 1);
    assert_eq!(slow[0].available_at_ns, config.start_ns + 15 * MINUTE);
    assert_eq!(
        slow[0].bars_by_symbol["SPY"].volume,
        num::Decimal::from(1500)
    );
    assert_eq!(
        outcome
            .manifest
            .acquired_subscriptions
            .iter()
            .filter(|s| s.data_type == "bars")
            .count(),
        1
    );
}

#[test]
fn incomplete_slow_window_reports_missing_instead_of_partial_ohlcv() {
    let mut config = config("15min");
    config.data_source_bar_interval = "1min".into();
    config.symbols = vec!["SPY".into()];
    let events = (0..15)
        .filter(|minute| *minute != 7)
        .map(|minute| bar("SPY", config.start_ns + minute * MINUTE))
        .collect();
    let frames = assemble_study_frames(
        &config,
        &fixture(&config, events),
        &SyntheticDataOptions::default(),
    )
    .unwrap();
    assert!(frames[0].bars_by_symbol.is_empty());
    assert_eq!(frames[0].missing_symbols, ["SPY"]);
}

#[test]
fn fitted_outputs_are_stamped_with_actual_training_execution() {
    let mut config = config("1min");
    config.symbols = vec!["SPY".into()];
    sync_study_subscriptions(&mut config);
    let mut observer = Observer::default();
    observer.outputs.insert("fit".into(), serde_json::json!({"kind":"fitted_state","training_job_id":"untrusted-author-claim","method":"normalization","training_start_ns":config.start_ns,"training_end_ns":config.end_ns,"available_at_ns":config.end_ns,"seed":0,"parameters":{},"weights":{"mean":10.0}}));
    let outcome = run(
        &config,
        &fixture(&config, vec![bar("SPY", config.start_ns)]),
        observer,
    );
    assert_eq!(
        outcome.exploratory_outputs["observer"]["fit"]["training_job_id"],
        config.job_id
    );
}
