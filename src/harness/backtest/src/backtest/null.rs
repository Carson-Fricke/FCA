//! Signal-destruction null transform for replay fixtures.
//!
//! Ordinary permutation shuffles individual per-symbol close-to-close log returns;
//! block permutation shuffles contiguous return blocks and preserves dependence
//! inside each block. Both move return magnitudes across calendar timestamps:
//! neither preserves the original calendar volatility schedule or cross-symbol
//! relationships. Symbols are independently randomized. Source timestamps,
//! sizes, and bar shape relative to close remain fixed; bars and ticks are
//! rescaled to the synthetic close path with checked scale-18 arithmetic.
//! A legitimate random draw may reproduce the original ordering. Designs with
//! fewer than two movable blocks, no returns, or a constant return sequence
//! fail preflight rather than masquerading as signal destruction.

use std::collections::{BTreeMap, BTreeSet};

use num::Decimal18;

use crate::sha256::Sha256;

use super::config::{BacktestConfig, NullTransformConfig};
use super::errors::BacktestError;
use super::fixture::{MarketEvent, ReplayFingerprints, ReplayFixture};
use super::rng::PythonRandom;

/// Apply the configured null transform to one replay fixture.
///
/// A configuration without a `null` block is an exact no-op. Otherwise every
/// symbol that carries events must also carry bars, and its bar OHLC and tick
/// prices are rescaled by the transformed close path. Timestamps, identifiers,
/// volumes, sizes, and event ordering are preserved, so session windows and
/// coverage stay consistent; the replay fingerprints are recomputed from the
/// transformed events.
///
/// # Errors
///
/// Returns [`BacktestError::DataPreflight`] when a symbol has events but no
/// bars, when a bar close is not positive (the log return is undefined), when a
/// bid, ask, or trade price is not positive (a tick has no rescaled value), or
/// when a rescaled price is not representable at scale 18.
pub fn apply_null_transform(
    configuration: &BacktestConfig,
    fixture: &ReplayFixture,
) -> Result<ReplayFixture, BacktestError> {
    let Some(null) = &configuration.null_transform else {
        return Ok(fixture.clone());
    };
    null.validate()?;

    let mut bar_indices: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    let mut symbols: BTreeSet<String> = BTreeSet::new();
    for (index, event) in fixture.events.iter().enumerate() {
        symbols.insert(event.symbol().to_string());
        if matches!(event, MarketEvent::Bar { .. }) {
            bar_indices
                .entry(event.symbol().to_string())
                .or_default()
                .push(index);
        }
    }
    if let Some(stream) = &fixture.stream {
        symbols.extend(stream.evidence.symbols.iter().cloned());
    }
    if symbols.is_empty() {
        return Err(BacktestError::DataPreflight(
            "null permutation requires a nonempty return series".into(),
        ));
    }
    for symbol in &symbols {
        if !bar_indices.contains_key(symbol) {
            return Err(BacktestError::DataPreflight(format!(
                "null permutation requires bar data for symbol {symbol:?}, which only has tick events"
            )));
        }
    }

    // One (timestamp, synthetic close, original close) reference per bar, in
    // time order, plus the per-event pair used to rescale each bar.
    let mut references: BTreeMap<String, Vec<(i64, Decimal18, Decimal18)>> = BTreeMap::new();
    let mut bar_scales: BTreeMap<usize, (Decimal18, Decimal18)> = BTreeMap::new();
    for (symbol, indices) in &bar_indices {
        let mut ordered = indices.clone();
        ordered.sort_by_key(|&index| {
            (
                fixture.events[index].timestamp_ns(),
                fixture.events[index].sequence(),
            )
        });
        let closes: Vec<(i64, Decimal18)> = ordered
            .iter()
            .map(|&index| {
                let MarketEvent::Bar {
                    timestamp_ns,
                    close,
                    ..
                } = &fixture.events[index]
                else {
                    unreachable!("bar index points at a bar event");
                };
                (*timestamp_ns, *close)
            })
            .collect();
        for (timestamp_ns, close) in &closes {
            if !close.is_positive() {
                return Err(BacktestError::DataPreflight(format!(
                    "null permutation requires positive bar closes for symbol \
                     {symbol:?} at {timestamp_ns}"
                )));
            }
        }
        let synthetic_closes = permuted_close_path(symbol, &closes, null)?;
        let mut reference = Vec::with_capacity(closes.len());
        for (position, &(timestamp_ns, original)) in closes.iter().enumerate() {
            let synthetic = synthetic_closes[position];
            bar_scales.insert(ordered[position], (synthetic, original));
            reference.push((timestamp_ns, synthetic, original));
        }
        references.insert(symbol.clone(), reference);
    }

    if let Some(source) = &fixture.stream {
        let reporter = source.reporter.clone();
        let references = std::sync::Arc::new(references);
        let events = fixture
            .events
            .iter()
            .cloned()
            .map(|event| rescale_stream_event(event, &references))
            .collect::<Result<Vec<_>, _>>()?;
        let mut evidence = source.evidence.clone();
        evidence.fingerprints.row = None;
        evidence.fingerprints.data = crate::sha256::digest_hex(
            format!(
                "fca:null-replay\0{}\0{}\0{}\0{}",
                source.evidence.fingerprints.data, null.mode, null.block_size, null.seed
            )
            .as_bytes(),
        );
        let coverage = source.coverage.clone();
        let expected_count = source.expected_count;
        let source = source.clone();
        let factory =
            std::sync::Arc::new(move || {
                let references = references.clone();
                Ok(Box::new(source.open()?.map(move |event| {
                    event.and_then(|event| rescale_stream_event(event, &references))
                })) as super::stream::Events)
            });
        let stream = std::sync::Arc::new(super::stream::StreamedReplay::new(
            factory,
            evidence,
            coverage,
            expected_count,
            reporter,
        ));
        let sessions = fixture.sessions.clone();
        return Ok(ReplayFixture {
            fingerprints: stream.evidence.fingerprints.clone(),
            stream: Some(stream),
            events,
            sessions,
            source_id: fixture.source_id.clone(),
            symbols: fixture.symbols.clone(),
            expected_data_fingerprint: fixture.expected_data_fingerprint.clone(),
        });
    }
    let mut events = fixture.events.clone();
    for (index, event) in events.iter_mut().enumerate() {
        match event {
            MarketEvent::Bar {
                symbol,
                open,
                high,
                low,
                close,
                ..
            } => {
                let (synthetic, original) = bar_scales[&index];
                *open = scale_price(*open, synthetic, original, symbol)?;
                *high = scale_price(*high, synthetic, original, symbol)?;
                *low = scale_price(*low, synthetic, original, symbol)?;
                *close = scale_price(*close, synthetic, original, symbol)?;
            }
            MarketEvent::Quote {
                symbol,
                timestamp_ns,
                bid,
                ask,
                ..
            } => {
                require_positive_tick(*bid, "bid", symbol, *timestamp_ns)?;
                require_positive_tick(*ask, "ask", symbol, *timestamp_ns)?;
                let (synthetic, original) = tick_reference(&references[symbol], *timestamp_ns);
                *bid = scale_price(*bid, synthetic, original, symbol)?;
                *ask = scale_price(*ask, synthetic, original, symbol)?;
            }
            MarketEvent::Trade {
                symbol,
                timestamp_ns,
                price,
                ..
            } => {
                require_positive_tick(*price, "trade price", symbol, *timestamp_ns)?;
                let (synthetic, original) = tick_reference(&references[symbol], *timestamp_ns);
                *price = scale_price(*price, synthetic, original, symbol)?;
            }
        }
    }

    let fingerprints = ReplayFingerprints::of_events(&events);
    Ok(ReplayFixture {
        stream: None,
        sessions: fixture.sessions.clone(),
        events,
        source_id: fixture.source_id.clone(),
        symbols: fixture.symbols.clone(),
        fingerprints,
        expected_data_fingerprint: fixture.expected_data_fingerprint.clone(),
    })
}

/// Reject a non-positive tick price. A tick is rescaled multiplicatively, so a
/// zero or negative price has no meaningful rescaled value; it must fail the
/// preflight rather than pass through as a silent zero.
fn require_positive_tick(
    price: Decimal18,
    field: &str,
    symbol: &str,
    timestamp_ns: i64,
) -> Result<(), BacktestError> {
    if price.is_positive() {
        return Ok(());
    }
    Err(BacktestError::DataPreflight(format!(
        "null permutation requires a positive {field} for symbol {symbol:?} at {timestamp_ns}"
    )))
}

/// Rebuild one symbol's synthetic close path from its permuted return blocks.
fn permuted_close_path(
    symbol: &str,
    closes: &[(i64, Decimal18)],
    null: &NullTransformConfig,
) -> Result<Vec<Decimal18>, BacktestError> {
    if closes.len() < 2 {
        return Err(BacktestError::DataPreflight(format!(
            "null permutation requires observed returns for symbol {symbol:?}"
        )));
    }
    let mut returns: Vec<f64> = Vec::with_capacity(closes.len().saturating_sub(1));
    for window in closes.windows(2) {
        let previous = window[0].1.to_f64();
        let current = window[1].1.to_f64();
        let value = (current / previous).ln();
        if !value.is_finite() {
            return Err(BacktestError::DataPreflight(format!(
                "null permutation requires finite log returns for symbol {symbol:?}"
            )));
        }
        returns.push(value);
    }
    let block_size = null.block_size as usize;
    let mut blocks: Vec<Vec<f64>> = returns.chunks(block_size).map(<[f64]>::to_vec).collect();
    if blocks.len() < 2 {
        return Err(BacktestError::DataPreflight(format!("null permutation requires at least two return blocks for symbol {symbol:?}; {} returns with block_size={block_size}", returns.len())));
    }
    if returns.iter().all(|value| *value == returns[0]) {
        return Err(BacktestError::DataPreflight(format!(
            "null permutation cannot change a constant return series for symbol {symbol:?}"
        )));
    }
    shuffle_blocks(&mut blocks, symbol, null.seed);
    let permuted: Vec<f64> = blocks.into_iter().flatten().collect();

    let mut synthetic = Vec::with_capacity(closes.len());
    synthetic.push(closes[0].1);
    let mut path = closes[0].1.to_f64();
    for value in &permuted {
        path *= value.exp();
        synthetic.push(Decimal18::from_f64(path).map_err(|error| {
            BacktestError::DataPreflight(format!(
                "null permutation produced an unrepresentable close for symbol \
                 {symbol:?}: {error}"
            ))
        })?);
    }
    Ok(synthetic)
}

fn rescale_stream_event(
    event: MarketEvent,
    references: &BTreeMap<String, Vec<(i64, Decimal18, Decimal18)>>,
) -> Result<MarketEvent, BacktestError> {
    let mut event = event;
    let symbol = event.symbol().to_string();
    let timestamp = event.timestamp_ns();
    let (synthetic, original) = tick_reference(&references[&symbol], timestamp);
    match &mut event {
        MarketEvent::Bar {
            open,
            high,
            low,
            close,
            ..
        } => {
            *open = scale_price(*open, synthetic, original, &symbol)?;
            *high = scale_price(*high, synthetic, original, &symbol)?;
            *low = scale_price(*low, synthetic, original, &symbol)?;
            *close = scale_price(*close, synthetic, original, &symbol)?;
        }
        MarketEvent::Quote { bid, ask, .. } => {
            require_positive_tick(*bid, "bid", &symbol, timestamp)?;
            require_positive_tick(*ask, "ask", &symbol, timestamp)?;
            *bid = scale_price(*bid, synthetic, original, &symbol)?;
            *ask = scale_price(*ask, synthetic, original, &symbol)?;
        }
        MarketEvent::Trade { price, .. } => {
            require_positive_tick(*price, "trade price", &symbol, timestamp)?;
            *price = scale_price(*price, synthetic, original, &symbol)?;
        }
    }
    Ok(event)
}

/// Fisher-Yates shuffle driven by the domain-separated symbol generator.
fn shuffle_blocks(blocks: &mut [Vec<f64>], symbol: &str, seed: u64) {
    let mut hasher = Sha256::new();
    hasher.update(b"fca:null-block-permutation:v1\0");
    hasher.update(&seed.to_le_bytes());
    hasher.update(symbol.as_bytes());
    let digest = hasher.finalize();
    let mut rng = PythonRandom::from_bytes(&digest);
    for index in (1..blocks.len()).rev() {
        let swap = rng.randbelow((index + 1) as u32) as usize;
        blocks.swap(index, swap);
    }
}

/// The scale pair of the last bar at or before `timestamp_ns`, or the first
/// bar's pair when the tick precedes every bar.
fn tick_reference(
    reference: &[(i64, Decimal18, Decimal18)],
    timestamp_ns: i64,
) -> (Decimal18, Decimal18) {
    let position = reference.partition_point(|(bar_ts, _, _)| *bar_ts <= timestamp_ns);
    if position == 0 {
        (reference[0].1, reference[0].2)
    } else {
        let (_, synthetic, original) = reference[position - 1];
        (synthetic, original)
    }
}

/// Rescale one exact price by `synthetic / original` with one rounding.
fn scale_price(
    price: Decimal18,
    synthetic: Decimal18,
    original: Decimal18,
    symbol: &str,
) -> Result<Decimal18, BacktestError> {
    price.checked_mul_div(synthetic, original).map_err(|error| {
        BacktestError::DataPreflight(format!(
            "null permutation could not rescale a price for symbol {symbol:?}: {error}"
        ))
    })
}
