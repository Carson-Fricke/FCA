use super::*;
use crate::backtest::errors::ONE_DAY_NS;
use crate::backtest::fixture::{days_from_civil, synthetic_fixture, MarketEvent};

fn config() -> BacktestConfig {
    let start = days_from_civil(2024, 1, 1) * ONE_DAY_NS;
    let end = days_from_civil(2024, 1, 3) * ONE_DAY_NS;
    BacktestConfig::new("run", start, end, 7).expect("config")
}

#[test]
fn preflight_summarizes_the_synthetic_fixture() {
    let configuration = config();
    let fixture =
        synthetic_fixture(&configuration, &SyntheticDataOptions::default()).expect("fixture");
    let preflight = preflight_data(&configuration, &fixture).expect("preflight");
    assert_eq!(preflight.source_id, "synthetic");
    assert_eq!(preflight.session_count, 2);
    assert_eq!(preflight.event_count, 36);
    assert_eq!(preflight.quote_count, 24);
    assert_eq!(preflight.trade_count, 12);
    assert_eq!(preflight.symbols, vec!["SPY".to_string()]);
}

#[test]
fn preflight_rejects_out_of_window_events() {
    let configuration = config();
    let mut fixture =
        synthetic_fixture(&configuration, &SyntheticDataOptions::default()).expect("fixture");
    if let Some(MarketEvent::Quote { timestamp_ns, .. }) = fixture.events.first_mut() {
        *timestamp_ns = configuration.start_ns - 1;
    }
    assert!(matches!(
        preflight_data(&configuration, &fixture),
        Err(BacktestError::DataPreflight(_))
    ));
}

#[test]
fn resolve_defaults_to_synthetic() {
    let fixture = resolve_data_source(&config(), None, None).expect("fixture");
    assert_eq!(fixture.source_id, "synthetic");
}
