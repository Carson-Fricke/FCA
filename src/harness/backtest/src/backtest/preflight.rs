//! Data-source resolution and runnability preflight for backtest runs.
//!
//! Port of `harness.runtime.backtest.preflight`. The local Parquet lake reader
//! lives in the Rust data layer (another epic), so the staged resolver handles
//! the deterministic synthetic fixture and an injected [`ReplayFixture`]; the
//! path and stream-mapping sources slot into the same entry point once the data
//! layer lands.

use super::config::BacktestConfig;
use super::errors::BacktestError;
use super::fixture::{synthetic_fixture, DataPreflight, ReplayFixture, SyntheticDataOptions};

/// Validate one fixture against a run and return assurance evidence.
///
/// # Errors
///
/// Returns [`BacktestError::DataPreflight`] when the fixture is empty, misses a
/// configured symbol, replays an out-of-window event, or fails the expected
/// fingerprint check.
pub fn preflight_data(
    configuration: &BacktestConfig,
    fixture: &ReplayFixture,
) -> Result<DataPreflight, BacktestError> {
    if !configuration.is_replay() {
        return Err(BacktestError::Configuration(
            "configuration must be a validated BacktestConfig".to_string(),
        ));
    }
    if fixture.sessions.is_empty() {
        return Err(BacktestError::DataPreflight(
            "a backtest fixture requires at least one session".to_string(),
        ));
    }
    if fixture.events.is_empty() {
        return Err(BacktestError::DataPreflight(
            "a backtest fixture must replay at least one event".to_string(),
        ));
    }
    let symbols = super::fixture::fixture_symbols(fixture);
    let wanted: Vec<String> = configuration
        .symbols
        .iter()
        .map(|symbol| symbol.to_uppercase())
        .collect();
    let missing: Vec<String> = wanted
        .iter()
        .filter(|symbol| !symbols.contains(symbol))
        .cloned()
        .collect();
    if configuration.shell != "study" && !missing.is_empty() {
        return Err(BacktestError::DataPreflight(format!(
            "fixture is missing configured symbols: {}",
            missing.join(", ")
        )));
    }
    for event in &fixture.events {
        let timestamp = event.timestamp_ns();
        if timestamp < configuration.start_ns || timestamp >= configuration.end_ns {
            return Err(BacktestError::DataPreflight(
                "an event falls outside the configured run window".to_string(),
            ));
        }
    }
    if let Some(expected) = &fixture.expected_data_fingerprint {
        if expected != &fixture.fingerprints.data {
            return Err(BacktestError::DataPreflight(
                "fixture data fingerprint does not match the expected fingerprint".to_string(),
            ));
        }
    }
    let quote_count = fixture
        .events
        .iter()
        .filter(|event| matches!(event, super::fixture::MarketEvent::Quote { .. }))
        .count();
    let trade_count = fixture
        .events
        .iter()
        .filter(|event| matches!(event, super::fixture::MarketEvent::Trade { .. }))
        .count();
    let bar_count = fixture
        .events
        .iter()
        .filter(|event| matches!(event, super::fixture::MarketEvent::Bar { .. }))
        .count();
    Ok(DataPreflight {
        source_id: fixture.source_id.clone(),
        session_count: fixture.sessions.len(),
        event_count: fixture.events.len(),
        quote_count,
        trade_count,
        bar_count,
        file_count: fixture.file_count(),
        symbols,
        fingerprints: fixture.fingerprints.clone(),
    })
}

/// Resolve the runner's `data_source` argument to one fixture.
///
/// # Errors
///
/// Returns [`BacktestError`] when the configuration or synthetic options are
/// invalid.
pub fn resolve_data_source(
    configuration: &BacktestConfig,
    data_source: Option<&ReplayFixture>,
    options: Option<&SyntheticDataOptions>,
) -> Result<ReplayFixture, BacktestError> {
    if !configuration.is_replay() {
        return Err(BacktestError::Configuration(
            "configuration must be a validated BacktestConfig".to_string(),
        ));
    }
    match data_source {
        Some(fixture) => Ok(fixture.clone()),
        None => {
            let chosen = options.cloned().unwrap_or_default();
            synthetic_fixture(configuration, &chosen)
        }
    }
}
