//! Full-resolution replay and result statistics survive bounded memory execution.
use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::*;
use backtest::backtest::fixture::synthetic_fixture;
use backtest::backtest::fixture::ReplayFixture;
use backtest::backtest::fixture::SyntheticDataOptions;
use backtest::backtest::history::History;
use backtest::backtest::identity::build_default_manifest;
use backtest::backtest::outcome::BacktestOutcome;
use backtest::backtest::preflight::preflight_data;
use backtest::backtest::stream::StreamedReplay;
use backtest::results::metrics::equity_curve_metrics;
use backtest::results::metrics::equity_curve_metrics_stream;
use backtest::results::metrics::EquityPoint;
use harness_trading::program::*;
use harness_trading::replay_errors::BacktestError;
use harness_trading::replay_errors::ONE_DAY_NS;
use num::Decimal18;
use std::collections::BTreeMap;
use std::sync::Arc;

struct Idle;
impl StrategyRuntime for Idle {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(
            ["quote", "trade"]
                .into_iter()
                .map(|kind| MarketSubscription {
                    data_type: kind.into(),
                    symbols: vec!["SPY".into()],
                    interval: None,
                })
                .collect(),
        )
    }
    fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }
    fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        Ok(Vec::new())
    }
    fn published_metrics(&self) -> BTreeMap<String, String> {
        BTreeMap::new()
    }
}
fn run(config: &BacktestConfig, fixture: &ReplayFixture) -> BacktestOutcome {
    let preflight = preflight_data(config, fixture).unwrap();
    let manifest = build_default_manifest(config, fixture, &preflight).unwrap();
    run_native_engine(
        config,
        fixture,
        &preflight,
        manifest,
        &EngineOptions::default(),
        vec![StrategyInstance {
            instance_id: "idle".into(),
            runtime: Box::new(Idle),
        }],
        &SyntheticDataOptions::default(),
    )
    .unwrap()
}
#[test]
fn streamed_replay_preserves_the_dispatch_hash_and_exact_valuation_history() {
    let mut config =
        BacktestConfig::new("streamed", 19723 * ONE_DAY_NS, 19725 * ONE_DAY_NS, 7).unwrap();
    config.data_subscriptions = Idle.subscriptions().unwrap();
    config.data_subscriptions_declared = true;
    let memory = synthetic_fixture(&config, &SyntheticDataOptions::default()).unwrap();
    let events = memory.events.clone();
    let factory = Arc::new(move || {
        Ok(Box::new(events.clone().into_iter().map(Ok)) as backtest::backtest::stream::Events)
    });
    let progress = Arc::new(std::sync::Mutex::new(Vec::new()));
    let received = progress.clone();
    let source = Arc::new(StreamedReplay::new(
        factory,
        preflight_data(&config, &memory).unwrap(),
        [
            ("quote".into(), "SPY".into()),
            ("trade".into(), "SPY".into()),
        ]
        .into(),
        Some(memory.events.len()),
        Some(Arc::new(move |snapshot| {
            received.lock().unwrap().push(snapshot);
            Ok(())
        })),
    ));
    let bars = Vec::new();
    let sessions = memory.sessions.clone();
    assert!(bars.is_empty());
    assert_eq!(source.evidence.event_count, memory.events.len());
    let stream = ReplayFixture {
        events: bars,
        stream: Some(source.clone()),
        sessions,
        source_id: memory.source_id.clone(),
        symbols: memory.symbols.clone(),
        fingerprints: source.evidence.fingerprints.clone(),
        expected_data_fingerprint: None,
    };
    let baseline = run(&config, &memory);
    let streamed = run(&config, &stream);
    assert_eq!(baseline.dispatch_hash, streamed.dispatch_hash);
    assert_eq!(baseline.market_event_count, streamed.market_event_count);
    let last = progress.lock().unwrap().last().cloned().unwrap();
    assert_eq!(
        last.stage,
        harness_contracts::progress::ExecutionStage::RunningBacktest
    );
    assert_eq!(last.rows, streamed.market_event_count);
    assert!(last.completed);
    assert_eq!(
        baseline
            .valuations
            .iter()
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap(),
        streamed
            .valuations
            .iter()
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap()
    );
}
#[test]
fn compressed_histories_preserve_every_row_and_exact_full_resolution_metrics() {
    let points: Vec<_> = (0..20_000)
        .map(|i| EquityPoint {
            timestamp_ns: i,
            equity: Decimal18::from_i64(100_000 + (i % 97) - (i % 13)),
        })
        .collect();
    let mut history = History::new(backtest::backtest::history::HistoryResources::new(
        2 * 1024 * 1024,
        std::sync::Arc::new(tempfile::NamedTempFile::new),
    ));
    for point in &points {
        history.push(*point).unwrap();
    }
    assert_eq!(history.len(), points.len());
    assert_eq!(
        history
            .iter()
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap(),
        points
    );
    assert_eq!(
        equity_curve_metrics_stream(|| history.iter()).unwrap(),
        equity_curve_metrics(&points)
    );
    let snapshot = history.clone();
    for point in &points[..8192] {
        history.push(*point).unwrap();
    }
    assert_eq!(snapshot.iter().unwrap().count(), points.len());
    assert_eq!(history.iter().unwrap().count(), points.len() + 8192);
}
