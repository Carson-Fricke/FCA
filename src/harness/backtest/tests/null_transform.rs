//! Signal-destruction null transform behavior and integration.
//!
//! The transform is a data boundary: it permutes per-symbol log-return blocks
//! and rescales bars and ticks before preflight, the manifest, and replay
//! fingerprints are derived, so every published record describes the
//! transformed events. These tests cover determinism, seed separation, the
//! absent-null no-op, OHLC coherence, tick reference scaling, symbol coverage,
//! and a real study job through the fake lake routes.

use std::collections::BTreeMap;

use backtest::backtest::config::{BacktestConfig, NullTransformConfig};
use backtest::backtest::engine::{
    run_backtest, ApproveAll, BacktestOptions, ImmediateFill, TradeBuyStrategy,
};
use backtest::backtest::engine_core::{
    EngineBarFrame, EngineMarket, EngineOptions, EngineOrder, MarketSubscription, StrategyInstance,
    StrategyRuntime, StrategyStart,
};
use backtest::backtest::errors::BacktestError;
use backtest::backtest::fixture::{
    ExchangeSession, MarketEvent, ReplayFingerprints, ReplayFixture,
};
use backtest::backtest::job::{parse_timestamp_ns, run_native_study_job_with_lake_client};
use backtest::backtest::null::apply_null_transform;
use harness_lakeclient::testing::{bars_stream, FakeBarRow, FakeLakeConfig, FakeLakeServer};
use harness_lakeclient::{BlockingLakeClient, LakeClientConfig};
use num::Decimal18;
use serde_json::{json, Value};

const MINUTE: i64 = 60_000_000_000;
const TOKEN: &str = "null-transform-token";

fn stamp(text: &str) -> i64 {
    parse_timestamp_ns(text).expect("timestamp")
}

fn base_config(seed: u64) -> BacktestConfig {
    let mut config = BacktestConfig::new(
        "null-transform",
        stamp("2024-01-02T14:30:00Z"),
        stamp("2024-01-02T15:00:00Z"),
        seed,
    )
    .expect("configuration");
    config.symbols = vec!["SPY".to_string(), "AAPL".to_string()];
    config.null_transform = Some(NullTransformConfig {
        mode: "block_permutation".to_string(),
        block_size: 2,
        seed,
    });
    config
}

fn bar(symbol: &str, timestamp_ns: i64, close: i64) -> MarketEvent {
    MarketEvent::Bar {
        event_id: format!("{symbol}-b-{timestamp_ns}"),
        timestamp_ns,
        sequence: 0,
        symbol: symbol.to_string(),
        open: Decimal18::from_i64(close),
        high: Decimal18::from_i64(close + 1),
        low: Decimal18::from_i64(close - 1),
        close: Decimal18::from_i64(close),
        volume: Decimal18::from_i64(100),
    }
}

fn quote(symbol: &str, timestamp_ns: i64, bid: i64, ask: i64) -> MarketEvent {
    MarketEvent::Quote {
        event_id: format!("{symbol}-q-{timestamp_ns}"),
        timestamp_ns,
        sequence: 1,
        symbol: symbol.to_string(),
        bid: Decimal18::from_i64(bid),
        ask: Decimal18::from_i64(ask),
        bid_quantity: Decimal18::from_i64(100),
        ask_quantity: Decimal18::from_i64(100),
        venue: "XNYS".to_string(),
    }
}

fn trade(symbol: &str, timestamp_ns: i64, price: i64, quantity: i64) -> MarketEvent {
    MarketEvent::Trade {
        event_id: format!("{symbol}-t-{timestamp_ns}"),
        timestamp_ns,
        sequence: 2,
        symbol: symbol.to_string(),
        price: Decimal18::from_i64(price),
        quantity: Decimal18::from_i64(quantity),
        venue: "XNYS".to_string(),
        trade_id: format!("trade-{timestamp_ns}"),
    }
}

fn sample_events(config: &BacktestConfig) -> Vec<MarketEvent> {
    let start = config.start_ns;
    vec![
        quote("SPY", start, 9, 10),
        bar("SPY", start + MINUTE, 10),
        bar("SPY", start + 2 * MINUTE, 11),
        trade("SPY", start + 2 * MINUTE + 1, 11, 5),
        bar("SPY", start + 3 * MINUTE, 13),
        bar("SPY", start + 4 * MINUTE, 12),
        quote("SPY", start + 5 * MINUTE, 12, 13),
        quote("AAPL", start, 19, 20),
        bar("AAPL", start + MINUTE, 20),
        bar("AAPL", start + 2 * MINUTE, 21),
        bar("AAPL", start + 3 * MINUTE, 19),
        bar("AAPL", start + 4 * MINUTE, 22),
        trade("AAPL", start + 5 * MINUTE, 22, 5),
    ]
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

fn run_config(config: &BacktestConfig, fixture: &ReplayFixture) -> String {
    run_backtest(
        config,
        Some(fixture),
        &BacktestOptions::default(),
        &mut TradeBuyStrategy::default(),
        &mut ApproveAll,
        &mut ImmediateFill,
    )
    .expect("deterministic run")
    .artifact_json()
}

#[test]
fn engine_parses_emits_and_hashes_the_optional_null_block() {
    let data = json!({"symbols": ["SPY"],
        "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}]});
    let document = json!({"shell": "backtest", "job_id": "null-parsed",
        "data": data.clone(),
        "null": {"mode": "block_permutation", "block_size": 5, "seed": 13}});
    let canonical = config::replay::validate_document(&document).expect("canonical tree");
    let parsed = BacktestConfig::from_yaml(&document.to_string()).expect("engine parse");
    let null = parsed.null_transform.as_ref().expect("parsed null");
    assert_eq!(null.mode, "block_permutation");
    assert_eq!(null.block_size, 5);
    assert_eq!(null.seed, 13);
    assert_eq!(parsed.canonical_json().to_serde_value(), canonical);
    assert_eq!(
        canonical["null"],
        json!({"mode": "block_permutation", "block_size": 5, "seed": 13})
    );

    let plain = BacktestConfig::from_yaml(
        "shell: backtest\njob_id: null-parsed\ndata:\n  symbols: [SPY]\n  subscriptions:\n    - data_type: quote\n      symbols: [SPY]\n",
    )
    .unwrap();
    assert!(plain.null_transform.is_none());
    assert!(plain
        .canonical_json()
        .to_serde_value()
        .get("null")
        .is_none());
    assert_ne!(plain.configuration_hash(), parsed.configuration_hash());

    for invalid in [
        json!({"shell": "backtest", "job_id": "x", "data": data.clone(),
            "null": {"mode": "shuffle", "block_size": 2, "seed": 1}}),
        json!({"shell": "backtest", "job_id": "x", "data": data.clone(),
            "null": {"mode": "block_permutation", "block_size": 0, "seed": 1}}),
        json!({"shell": "backtest", "job_id": "x", "data": data.clone(),
            "null": {"mode": "block_permutation", "block_size": 2, "seed": -1}}),
        json!({"shell": "backtest", "job_id": "x", "data": data.clone(),
            "null": {"mode": "block_permutation", "block_size": 2}}),
    ] {
        assert!(
            BacktestConfig::from_yaml(&invalid.to_string()).is_err(),
            "engine must reject {invalid}"
        );
    }
}

#[test]
fn absent_null_is_an_exact_no_op_and_keeps_the_baseline_run() {
    let mut config = base_config(11);
    config.null_transform = None;
    let original = fixture(&config, sample_events(&config));
    let no_op = apply_null_transform(&config, &original).expect("no-op transform");
    assert_eq!(no_op, original, "absent null returns the identical fixture");

    let baseline = run_config(&config, &original);
    let repeated = run_config(&config, &no_op);
    assert_eq!(baseline, repeated);
}

#[test]
fn same_seed_is_deterministic_for_fixture_and_artifact() {
    let config = base_config(21);
    let original = fixture(&config, sample_events(&config));
    let first = apply_null_transform(&config, &original).expect("first transform");
    let second = apply_null_transform(&config, &original).expect("second transform");
    assert_eq!(first, second);
    assert_ne!(first, original, "the transform must change the fixture");
    assert_eq!(
        run_config(&config, &original),
        run_config(&config, &original),
        "the same config and seed replay byte-identical artifacts"
    );
}

#[test]
fn distinct_seeds_change_transformed_data_and_artifacts() {
    let start = stamp("2024-01-02T14:30:00Z");
    let mut events = Vec::new();
    for (index, close) in [10, 11, 9, 12, 13, 11, 14, 15, 13, 16, 14, 17]
        .iter()
        .enumerate()
    {
        events.push(bar("SPY", start + (index as i64 + 1) * MINUTE, *close));
        events.push(bar("AAPL", start + (index as i64 + 1) * MINUTE, close + 10));
    }
    let mut previous: Option<(ReplayFixture, String)> = None;
    for seed in [1u64, 2, 3] {
        let mut config = base_config(seed);
        config.null_transform = Some(NullTransformConfig {
            mode: "block_permutation".into(),
            block_size: 2,
            seed,
        });
        let fixture = fixture(&config, events.clone());
        let transformed = apply_null_transform(&config, &fixture).expect("transform");
        assert_ne!(transformed, fixture, "seed {seed} transformed the fixture");
        let artifact = run_config(&config, &fixture);
        if let Some((prior_fixture, prior_artifact)) = &previous {
            assert_ne!(
                &transformed, prior_fixture,
                "seed {seed} must differ from the previous seed"
            );
            assert_ne!(
                &artifact, prior_artifact,
                "seed {seed} must change the run artifact"
            );
        }
        previous = Some((transformed, artifact));
    }
}

#[test]
fn transformed_bars_stay_ohlc_coherent_and_keep_their_identity() {
    let config = base_config(31);
    let original = fixture(&config, sample_events(&config));
    let transformed = apply_null_transform(&config, &original).expect("transform");
    assert_eq!(transformed.events.len(), original.events.len());
    let mut changed = 0usize;
    for (before, after) in original.events.iter().zip(&transformed.events) {
        let (
            MarketEvent::Bar {
                event_id,
                timestamp_ns,
                sequence,
                symbol,
                open: before_open,
                close: before_close,
                volume,
                ..
            },
            MarketEvent::Bar {
                event_id: after_event_id,
                timestamp_ns: after_timestamp,
                sequence: after_sequence,
                symbol: after_symbol,
                open,
                high,
                low,
                close,
                volume: after_volume,
            },
        ) = (before, after)
        else {
            continue;
        };
        assert_eq!(event_id, after_event_id);
        assert_eq!(timestamp_ns, after_timestamp);
        assert_eq!(sequence, after_sequence);
        assert_eq!(symbol, after_symbol);
        assert_eq!(volume, after_volume, "volume is never rescaled");
        assert_eq!(
            before_open, before_close,
            "fixture bars open at their close"
        );
        assert_eq!(open, close, "open and close share one exact scale");
        assert!(high >= low, "high {high:?} < low {low:?}");
        assert!(high >= open && high >= close);
        assert!(low <= open && low <= close);
        if before != after {
            changed += 1;
        }
    }
    assert!(changed > 0, "at least one bar must be rescaled");
}

#[test]
fn ticks_scale_by_the_last_bar_at_or_before_the_tick() {
    let config = base_config(41);
    let original = fixture(&config, sample_events(&config));
    let transformed = apply_null_transform(&config, &original).expect("transform");

    // Per symbol, collect the original and transformed bar closes in time order.
    let mut references: BTreeMap<String, Vec<(i64, Decimal18, Decimal18)>> = BTreeMap::new();
    for (before, after) in original.events.iter().zip(&transformed.events) {
        if let (
            MarketEvent::Bar {
                symbol,
                timestamp_ns,
                close,
                ..
            },
            MarketEvent::Bar {
                close: after_close, ..
            },
        ) = (before, after)
        {
            references.entry(symbol.clone()).or_default().push((
                *timestamp_ns,
                *close,
                *after_close,
            ));
        }
    }
    let tick_reference = |symbol: &str, timestamp_ns: i64| {
        let bars = &references[symbol];
        let position = bars.partition_point(|(bar_ts, _, _)| *bar_ts <= timestamp_ns);
        let (_, original_close, synthetic_close) = if position == 0 {
            bars[0]
        } else {
            bars[position - 1]
        };
        (original_close, synthetic_close)
    };

    let mut checked = 0usize;
    let mut rescaled = 0usize;
    for (before, after) in original.events.iter().zip(&transformed.events) {
        match (before, after) {
            (
                MarketEvent::Quote {
                    symbol,
                    timestamp_ns,
                    bid,
                    ask,
                    ..
                },
                MarketEvent::Quote {
                    bid: after_bid,
                    ask: after_ask,
                    ..
                },
            ) => {
                let (original_close, synthetic_close) = tick_reference(symbol, *timestamp_ns);
                assert_eq!(
                    *after_bid,
                    bid.checked_mul_div(synthetic_close, original_close)
                        .unwrap()
                );
                assert_eq!(
                    *after_ask,
                    ask.checked_mul_div(synthetic_close, original_close)
                        .unwrap()
                );
                if after_bid != bid || after_ask != ask {
                    rescaled += 1;
                }
                checked += 1;
            }
            (
                MarketEvent::Trade {
                    symbol,
                    timestamp_ns,
                    price,
                    ..
                },
                MarketEvent::Trade {
                    price: after_price, ..
                },
            ) => {
                let (original_close, synthetic_close) = tick_reference(symbol, *timestamp_ns);
                assert_eq!(
                    *after_price,
                    price
                        .checked_mul_div(synthetic_close, original_close)
                        .unwrap()
                );
                if after_price != price {
                    rescaled += 1;
                }
                checked += 1;
            }
            _ => {}
        }
    }
    assert!(checked >= 5, "every tick reference must be exercised");
    assert!(rescaled > 0, "ticks after the first bar must move");
    // A tick before a symbol's first bar uses that first bar's factor, which is
    // exactly one because the first synthetic close is pinned to the original.
    let (
        Some(MarketEvent::Quote { bid: first_bid, .. }),
        Some(MarketEvent::Quote {
            bid: after_first_bid,
            ..
        }),
    ) = (original.events.first(), transformed.events.first())
    else {
        panic!("first event is a quote");
    };
    assert_eq!(first_bid, after_first_bid);
}

#[test]
fn symbols_with_events_but_no_bars_are_rejected_by_name() {
    let config = base_config(51);
    let events = vec![
        bar("SPY", config.start_ns + MINUTE, 10),
        quote("AAPL", config.start_ns, 19, 20),
    ];
    let error = apply_null_transform(&config, &fixture(&config, events)).unwrap_err();
    assert!(error.to_string().contains("AAPL"), "{error}");
    assert!(error.to_string().contains("bar data"), "{error}");
}

#[test]
fn non_positive_closes_are_rejected_instead_of_leaking_nan() {
    let config = base_config(61);
    let original = fixture(
        &config,
        vec![
            bar("SPY", config.start_ns + MINUTE, 10),
            bar("SPY", config.start_ns + 2 * MINUTE, 11),
            bar("AAPL", config.start_ns + MINUTE, 20),
            bar("AAPL", config.start_ns + 2 * MINUTE, 21),
        ],
    );
    let mut broken = original.clone();
    if let MarketEvent::Bar { close, .. } = &mut broken.events[1] {
        *close = Decimal18::ZERO;
    }
    let error = apply_null_transform(&config, &broken).unwrap_err();
    assert!(error.to_string().contains("positive bar closes"), "{error}");
}

#[test]
fn non_positive_tick_prices_fail_preflight_by_name_before_rescaling() {
    let config = base_config(62);
    let bars = [
        bar("SPY", config.start_ns + MINUTE, 10),
        bar("SPY", config.start_ns + 2 * MINUTE, 11),
    ];
    let broken_ticks = [
        ("bid", quote("SPY", config.start_ns, 0, 10)),
        ("ask", quote("SPY", config.start_ns, 9, 0)),
        ("trade price", trade("SPY", config.start_ns, 0, 5)),
        ("trade price", trade("SPY", config.start_ns, -3, 5)),
    ];
    for (field, tick) in broken_ticks {
        let mut events = bars.to_vec();
        events.push(tick);
        let error = apply_null_transform(&config, &fixture(&config, events)).unwrap_err();
        assert!(
            matches!(&error, BacktestError::DataPreflight(_)),
            "a non-positive {field} must be a DataPreflight error, got {error:?}"
        );
        assert!(error.to_string().contains("positive"), "{error}");
        assert!(error.to_string().contains(field), "{error}");
    }
}

struct NoopProgram;

impl StrategyRuntime for NoopProgram {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(vec![MarketSubscription {
            data_type: "bars".into(),
            symbols: vec!["SPY".into()],
            interval: Some("1min".into()),
        }])
    }

    fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }

    fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        Ok(Vec::new())
    }

    fn on_bar_frame(&mut self, _: &EngineBarFrame) -> Result<(), BacktestError> {
        Ok(())
    }
}

fn client(server: &FakeLakeServer) -> BlockingLakeClient {
    BlockingLakeClient::new(
        LakeClientConfig::new(server.base_url(), TOKEN).expect("lake client config"),
    )
    .expect("blocking lake client")
}

fn study_config(seed: u64) -> BacktestConfig {
    let mut config = base_config(seed);
    config.shell = "study".to_string();
    config.symbols = vec!["SPY".to_string()];
    config.data_bar_interval = "1min".to_string();
    config.end_ns = config.start_ns + 12 * MINUTE;
    config.null_transform.as_mut().unwrap().block_size = 1;
    config
}

fn run_study(config: &BacktestConfig, name: &str) -> Value {
    let start = config.start_ns;
    let bars: Vec<FakeBarRow> = [10, 12, 11, 13, 12, 15, 14, 16, 15, 17, 16]
        .iter()
        .enumerate()
        .map(|(index, close)| {
            FakeBarRow::new(
                start + (index as i64 + 1) * MINUTE,
                &(close - 1).to_string(),
                &(close + 1).to_string(),
                &(close - 1).to_string(),
                &close.to_string(),
                "100",
            )
        })
        .collect();
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([("bars".to_string(), bars_stream("SPY", &bars))]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let directory = std::env::temp_dir().join(format!("backtest-null-transform-{name}"));
    let _ = std::fs::remove_dir_all(&directory);
    let (outcome, _) = run_native_study_job_with_lake_client(
        config,
        &EngineOptions::default(),
        Some(&directory),
        vec![StrategyInstance {
            instance_id: "null-study".to_string(),
            runtime: Box::new(NoopProgram),
        }],
        &client(&server),
    )
    .expect("study job");
    assert!(outcome.completed());
    serde_json::from_str(&outcome.artifact_json()).expect("artifact json")
}

/// The run identity fields that must not depend on the ephemeral fake-lake port.
fn run_identity(artifact: &Value) -> Value {
    json!({
        "configuration_hash": artifact["manifest"]["configuration_hash"],
        "manifest_replay_data_fingerprint": artifact["manifest"]["replay_data_fingerprint"],
        "manifest_replay_row_fingerprint": artifact["manifest"]["replay_row_fingerprint"],
        "preflight_replay_data_fingerprint": artifact["preflight"]["replay_data_fingerprint"],
        "dispatch_hash": artifact["dispatch_hash"],
    })
}

#[test]
fn study_job_consumes_the_null_transform_before_manifest_and_replay() {
    let config = study_config(7);
    let first = run_study(&config, "seed-7-a");
    let second = run_study(&config, "seed-7-b");
    assert_eq!(
        run_identity(&first),
        run_identity(&second),
        "same seed is identical"
    );

    let mut other = config.clone();
    other.seed = 8;
    other.null_transform.as_mut().unwrap().seed = 8;
    let different = run_study(&other, "seed-8");
    assert_ne!(
        first["preflight"]["replay_data_fingerprint"],
        different["preflight"]["replay_data_fingerprint"],
        "a distinct permutation seed must change the replayed data"
    );

    let mut plain = config.clone();
    plain.null_transform = None;
    let baseline = run_study(&plain, "absent-null");
    assert_ne!(
        first["preflight"]["replay_data_fingerprint"],
        baseline["preflight"]["replay_data_fingerprint"],
        "the transformed fingerprint must differ from the raw lake data"
    );
    assert_ne!(
        first["manifest"]["configuration_hash"],
        baseline["manifest"]["configuration_hash"]
    );
    assert!(baseline["preflight"]["replay_data_fingerprint"].is_string());
    assert_eq!(
        first["preflight"]["bar_count"],
        baseline["preflight"]["bar_count"]
    );
}
