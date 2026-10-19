//! Validate replay market coverage before deterministic execution.

use super::config::BacktestConfig;
use super::errors::BacktestError;
use super::fixture::{DataPreflight, ReplayFixture};

/// Require at least one dispatchable completed frame for every requested bar symbol.
///
/// # Errors
/// Rejects raw bars whose intervals never complete within the run window.
pub fn validate_completed_bar_coverage(
    subscriptions: &[super::engine_core::MarketSubscription],
    frames: &[super::engine_core::EngineBarFrame],
) -> Result<(), BacktestError> {
    for input in subscriptions
        .iter()
        .filter(|input| input.data_type == "bars")
    {
        for symbol in &input.symbols {
            if !frames.iter().any(|frame| {
                input.interval.as_deref() == Some(frame.interval.as_str())
                    && frame.bars_by_symbol.contains_key(symbol)
            }) {
                return Err(BacktestError::DataPreflight(format!(
                    "fixture is missing requested bars/{symbol} completed frame coverage"
                )));
            }
        }
    }
    Ok(())
}

/// Require coverage for each exact symbol/type, not merely any stream.
///
/// # Errors
/// Rejects requested inputs with no rows in the replay window.
pub fn validate_subscription_coverage(
    fixture: &ReplayFixture,
    subscriptions: &[super::engine_core::MarketSubscription],
) -> Result<(), BacktestError> {
    for input in subscriptions {
        for symbol in &input.symbols {
            if !fixture.stream.as_ref().map_or_else(
                || {
                    fixture.events.iter().any(|event| {
                        event.symbol() == symbol
                            && matches!(
                                (input.data_type.as_str(), event),
                                ("quote", super::fixture::MarketEvent::Quote { .. })
                                    | ("trade", super::fixture::MarketEvent::Trade { .. })
                                    | ("bars", super::fixture::MarketEvent::Bar { .. })
                            )
                    })
                },
                |stream| stream.covers(&input.data_type, symbol),
            ) {
                return Err(BacktestError::DataPreflight(format!(
                    "fixture is missing requested {}/{} coverage",
                    input.data_type, symbol
                )));
            }
        }
    }
    Ok(())
}

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
    if let Some(stream) = &fixture.stream {
        if stream.bounds.is_some_and(|(first, last)| {
            first < configuration.start_ns || last >= configuration.end_ns
        }) {
            return Err(BacktestError::DataPreflight(
                "an event falls outside the configured run window".into(),
            ));
        }
        if configuration.shell != "study"
            && configuration
                .symbols
                .iter()
                .any(|symbol| !stream.evidence.symbols.contains(&symbol.to_uppercase()))
        {
            return Err(BacktestError::DataPreflight(
                "fixture is missing configured symbols".into(),
            ));
        }
        if let Some(expected) = &fixture.expected_data_fingerprint {
            if expected != &stream.evidence.fingerprints.data {
                return Err(BacktestError::DataPreflight(
                    "fixture data fingerprint does not match the expected fingerprint".into(),
                ));
            }
        }
        if stream.evidence.event_count == 0 {
            return Err(BacktestError::DataPreflight("empty replay".into()));
        }
        return Ok(stream.evidence.clone());
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
