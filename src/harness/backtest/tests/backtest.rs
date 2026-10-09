//! Exact arithmetic of the deterministic synthetic market fixture.

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::errors::ONE_DAY_NS;
use backtest::backtest::fixture::{synthetic_fixture, MarketEvent, SyntheticDataOptions};

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
