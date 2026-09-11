//! End-to-end backtest tests across the public kernel surface.

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine::{
    run_backtest, ApproveAll, BacktestOptions, ImmediateFill, TradeBuyStrategy,
};
use backtest::backtest::errors::ONE_DAY_NS;
use backtest::backtest::fixture::{synthetic_fixture, MarketEvent, SyntheticDataOptions};
use backtest::backtest::preflight::preflight_data;

fn config() -> BacktestConfig {
    // 2024-01-01T00:00:00Z for two days (Mon/Tue sessions).
    let start = 19_723 * ONE_DAY_NS;
    let end = 19_725 * ONE_DAY_NS;
    BacktestConfig::new("bt-e2e", start, end, 7).expect("config")
}

#[test]
fn synthetic_path_matches_the_python_reference() {
    let fixture = synthetic_fixture(&config(), &SyntheticDataOptions::default()).expect("fixture");
    assert_eq!(fixture.events.len(), 36);
    let MarketEvent::Quote { bid, ask, .. } = &fixture.events[0] else {
        panic!("first event must be a quote");
    };
    // Python: mid 450.00 + 1 tick = 450.01, half spread 0.005.
    assert_eq!(bid.to_string(), "450.005000000000000000");
    assert_eq!(ask.to_string(), "450.015000000000000000");
}

#[test]
fn backtest_is_deterministic_and_preflights_cleanly() {
    let configuration = config();
    let options = BacktestOptions::default();
    let fixture =
        synthetic_fixture(&configuration, &SyntheticDataOptions::default()).expect("fixture");
    let preflight = preflight_data(&configuration, &fixture).expect("preflight");
    assert_eq!(preflight.event_count, 36);

    let first = run_backtest(
        &configuration,
        Some(&fixture),
        &options,
        &mut TradeBuyStrategy::default(),
        &mut ApproveAll,
        &mut ImmediateFill,
    )
    .expect("first run");
    let second = run_backtest(
        &configuration,
        Some(&fixture),
        &options,
        &mut TradeBuyStrategy::default(),
        &mut ApproveAll,
        &mut ImmediateFill,
    )
    .expect("second run");
    assert!(first.completed());
    assert_eq!(first.order_count, 12);
    assert_eq!(first.fill_count, 12);
    assert_eq!(first.artifact_json(), second.artifact_json());
    assert_eq!(first.artifact_digest(), second.artifact_digest());
    assert_eq!(first.outcome_digest(), second.outcome_digest());
    assert_eq!(
        first.manifest.manifest_hash(),
        second.manifest.manifest_hash()
    );
    // The artifact document embeds the manifest and preflight records.
    let document = first.artifact_json();
    assert!(document.contains("\"manifest\""));
    assert!(document.contains("\"preflight\""));
    assert!(document.contains("\"market_events\":36"));
}
