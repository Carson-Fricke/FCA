use super::*;
use std::str::FromStr;

fn config() -> BacktestConfig {
    // 2024-01-01T00:00:00Z .. 2024-01-03T00:00:00Z (Mon/Tue sessions).
    let start = days_from_civil(2024, 1, 1) * ONE_DAY_NS;
    let end = days_from_civil(2024, 1, 3) * ONE_DAY_NS;
    BacktestConfig::new("run", start, end, 7).expect("config")
}

#[test]
fn weekday_matches_python() {
    assert_eq!(weekday(days_from_civil(2024, 1, 1)), 0); // Monday
    assert_eq!(weekday(days_from_civil(2024, 1, 2)), 1); // Tuesday
    assert_eq!(weekday(days_from_civil(2024, 1, 6)), 5); // Saturday
    assert_eq!(date_string(days_from_civil(2024, 1, 2)), "2024-01-02");
}

#[test]
fn synthetic_events_match_python_drift() {
    let fixture = synthetic_fixture(&config(), &SyntheticDataOptions::default()).expect("fixture");
    // Two weekday sessions, 12 quotes and 6 trades each.
    assert_eq!(fixture.events.len(), 36);
    let quotes: Vec<&MarketEvent> = fixture
        .events
        .iter()
        .filter(|event| matches!(event, MarketEvent::Quote { .. }))
        .collect();
    assert_eq!(quotes.len(), 24);
    // First mid: 450.00 + 1 * 0.01 = 450.01, bid 450.005.
    let MarketEvent::Quote { bid, .. } = quotes[0] else {
        unreachable!()
    };
    assert_eq!(bid.to_string(), "450.005000000000000000");
}

#[test]
fn synthetic_fixture_is_deterministic() {
    let left = synthetic_fixture(&config(), &SyntheticDataOptions::default()).expect("left");
    let right = synthetic_fixture(&config(), &SyntheticDataOptions::default()).expect("right");
    assert_eq!(left, right);
    assert_eq!(left.fingerprints, right.fingerprints);
}

#[test]
fn synthetic_event_order_matches_python_merge() {
    let fixture = synthetic_fixture(&config(), &SyntheticDataOptions::default()).expect("fixture");
    // Python interleaves the quote and trade streams by timestamp.
    assert!(matches!(fixture.events[0], MarketEvent::Quote { .. }));
    assert!(matches!(fixture.events[1], MarketEvent::Quote { .. }));
    assert!(matches!(fixture.events[2], MarketEvent::Trade { .. }));
    let MarketEvent::Trade { timestamp_ns, .. } = fixture.events[2] else {
        unreachable!()
    };
    // 2024-01-01T14:18:45Z, the first session's first trade.
    let expected = days_from_civil(2024, 1, 1) * ONE_DAY_NS
        + 14 * 3_600_000_000_000
        + 18 * 60_000_000_000
        + 45 * 1_000_000_000;
    assert_eq!(timestamp_ns, expected);
}

/// Golden fingerprints produced by the Python reference
/// (`harness.data.replay`) for the two-day synthetic fixture.
#[test]
fn synthetic_fingerprints_match_python_2day_golden() {
    let fixture = synthetic_fixture(&config(), &SyntheticDataOptions::default()).expect("fixture");
    assert_eq!(
        fixture.fingerprints.file,
        "769536b1681f0658f85cd2d8ddcd13fbe02cf779116b61344159bf5fb4b0d214"
    );
    assert_eq!(
        fixture.fingerprints.row,
        "9f91f184102f374ff0535dc91a0ab8233dcb08e8bb6cafa6b5020eb0dd41f232"
    );
    assert_eq!(
        fixture.fingerprints.data,
        "01b16450ec170ef34a6b52af3a887c00ae299f40bd2824d3c07f452e7f81970f"
    );
}

#[test]
fn python_decimal_renders_python_scale() {
    assert_eq!(python_decimal(Decimal18::from_str("0").unwrap(), 2), "0.00");
    assert_eq!(
        python_decimal(Decimal18::from_str("450.00").unwrap(), 2),
        "450.00"
    );
    assert_eq!(
        python_decimal(Decimal18::from_str("449.955").unwrap(), 3),
        "449.955"
    );
    assert_eq!(python_decimal(Decimal18::from_i64(100), 0), "100");
}

/// The canonical `config/harness/backtest.yaml` window (14 sessions).
fn canonical_config() -> BacktestConfig {
    let start =
        days_from_civil(2024, 1, 2) * ONE_DAY_NS + 14 * 3_600_000_000_000 + 30 * 60_000_000_000;
    let end = days_from_civil(2024, 1, 19) * ONE_DAY_NS + 21 * 3_600_000_000_000;
    let mut configuration =
        BacktestConfig::new("momentum-spy-2024-01", start, end, 1729).expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    configuration
}

/// Golden fingerprints produced by the Python reference for the canonical
/// `config/harness/backtest.yaml` synthetic fixture.
#[test]
fn synthetic_fingerprints_match_python_canonical_golden() {
    let fixture =
        synthetic_fixture(&canonical_config(), &SyntheticDataOptions::default()).expect("fixture");
    assert_eq!(fixture.events.len(), 252);
    assert_eq!(
        fixture.fingerprints.file,
        "769536b1681f0658f85cd2d8ddcd13fbe02cf779116b61344159bf5fb4b0d214"
    );
    assert_eq!(
        fixture.fingerprints.row,
        "7da175c63a4386bfa3e56ccce696f2c4e8f353ff81e97bf427950fcafc3361ec"
    );
    assert_eq!(
        fixture.fingerprints.data,
        "ec48813dd420956fcacd8d5428f8c2226b922956c5180e171f2e58d22db7c137"
    );
}
