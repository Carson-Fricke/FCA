//! Owns replay requirement normalization and causal bar-frame assembly; no execution or storage.
use crate::backtest::config::BacktestConfig;
use crate::backtest::fixture::MarketEvent;
use crate::backtest::fixture::ReplayFixture;
use crate::backtest::fixture::SyntheticDataOptions;
use harness_trading::program::*;
use harness_trading::replay_errors::BacktestError;
use std::collections::BTreeMap;
use std::collections::BTreeSet;

/// Validate and deduplicate an exact requirement union.
///
/// # Errors
/// Rejects invalid declarations and intervals unsupported by minute-source aggregation.
pub fn normalize_subscriptions(
    inputs: &[MarketSubscription],
) -> Result<Vec<MarketSubscription>, BacktestError> {
    let mut declarations = Vec::new();
    for input in inputs {
        let declaration = match (
            harness_contracts::DataType::parse(&input.data_type)
                .map_err(BacktestError::Configuration)?,
            &input.interval,
        ) {
            (harness_contracts::DataType::Bars, Some(interval)) => {
                harness_contracts::Subscription::Bars {
                    symbols: input.symbols.clone(),
                    interval: interval.clone(),
                }
            }
            (harness_contracts::DataType::Quote, None) => harness_contracts::Subscription::Quote {
                symbols: input.symbols.clone(),
            },
            (harness_contracts::DataType::Trade, None) => harness_contracts::Subscription::Trade {
                symbols: input.symbols.clone(),
            },
            _ => {
                return Err(BacktestError::Configuration(
                    "invalid subscription type/interval".into(),
                ))
            }
        };
        declarations.push(declaration);
    }
    let declarations = harness_contracts::canonical_subscriptions(declarations)
        .map_err(BacktestError::Configuration)?;
    let mut output = Vec::new();
    for input in declarations {
        let (data_type, symbols, interval) = match input {
            harness_contracts::Subscription::Bars { symbols, interval } => {
                replay_interval_ns(&interval)?;
                ("bars", symbols, Some(interval))
            }
            harness_contracts::Subscription::Quote { symbols } => ("quote", symbols, None),
            harness_contracts::Subscription::Trade { symbols } => ("trade", symbols, None),
        };
        output.push(MarketSubscription {
            data_type: data_type.into(),
            symbols,
            interval,
        });
    }
    Ok(output)
}

/// Supported completed bars: positive minute/hour widths, or one session day.
pub fn replay_interval_ns(interval: &str) -> Result<Option<i64>, BacktestError> {
    harness_contracts::completed_bar_interval_ns(interval).map_err(BacktestError::Configuration)
}

/// Canonical acquisition uses minute bars for every requested timeframe.
pub fn source_subscriptions(
    inputs: &[MarketSubscription],
    source_interval: &str,
) -> Result<Vec<MarketSubscription>, BacktestError> {
    let mut sources = inputs.to_vec();
    for input in &mut sources {
        if input.data_type == "bars" {
            input.interval = Some(source_interval.into());
        }
    }
    normalize_subscriptions(&sources)
}

/// Assemble every declared interval from the same causal source series.
pub fn assemble_subscription_frames(
    configuration: &BacktestConfig,
    fixture: &ReplayFixture,
    options: &SyntheticDataOptions,
    requirements: &[MarketSubscription],
) -> Result<Vec<EngineBarFrame>, BacktestError> {
    let bars = fixture
        .events
        .iter()
        .filter(|event| matches!(event, MarketEvent::Bar { .. }))
        .map(|event| market_event(event, options))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .filter_map(|event| match event {
            EngineMarket::Bar(bar) => Some(bar),
            _ => None,
        })
        .collect::<Vec<_>>();
    let mut frames = Vec::new();
    for input in requirements.iter().filter(|s| s.data_type == "bars") {
        let mut config = configuration.clone();
        config.symbols = input.symbols.clone();
        config.data_bar_interval = input.interval.clone().unwrap();
        let selected = bars
            .iter()
            .filter(|bar| input.symbols.contains(&bar.symbol))
            .cloned()
            .collect();
        frames.extend(assemble_bar_observations(&config, selected)?);
    }
    frames.sort_by(|a, b| {
        a.available_at_ns
            .cmp(&b.available_at_ns)
            .then_with(|| {
                (b.interval_end_ns - b.interval_start_ns)
                    .cmp(&(a.interval_end_ns - a.interval_start_ns))
            })
            .then_with(|| a.interval.cmp(&b.interval))
            .then_with(|| a.interval_start_ns.cmp(&b.interval_start_ns))
    });
    Ok(frames)
}

/// Backtest execution feeds: quote and trade for every declared input symbol.
pub fn execution_subscriptions(requirements: &[MarketSubscription]) -> Vec<MarketSubscription> {
    let mut symbols: Vec<String> = requirements
        .iter()
        .flat_map(|input| input.symbols.clone())
        .collect();
    symbols.sort();
    symbols.dedup();
    ["quote", "trade"]
        .into_iter()
        .map(|kind| MarketSubscription {
            data_type: kind.into(),
            symbols: symbols.clone(),
            interval: None,
        })
        .collect()
}

/// Resolve consumer requirements before acquisition, validating the optional echo.
///
/// # Errors
/// Rejects invalid inputs or an echo that differs from consumer requirements.
pub fn runtime_subscriptions(
    configuration: &BacktestConfig,
    strategies: &[StrategyInstance],
) -> Result<Vec<MarketSubscription>, BacktestError> {
    let mut inputs = Vec::new();
    for instance in strategies {
        inputs.extend(instance.runtime.subscriptions().ok_or_else(|| {
            BacktestError::Configuration("runtime must declare its subscriptions".into())
        })?);
    }
    let union = normalize_subscriptions(&inputs)?;
    validate_subscription_echo(configuration, &union)?;
    Ok(union)
}

pub(super) fn validate_subscription_echo(
    configuration: &BacktestConfig,
    union: &[MarketSubscription],
) -> Result<(), BacktestError> {
    if configuration.has_subscription_echo()
        && normalize_subscriptions(&configuration.data_subscriptions)? != union
    {
        return Err(BacktestError::Configuration(
            "data.subscriptions does not match runtime subscription union".into(),
        ));
    }
    Ok(())
}

/// Assemble bars in a mixed replay into atomic, complete intervals.
///
/// # Errors
///
/// Rejects invalid intervals, duplicates, unexpected bar symbols,
/// off-grid starts, and incompatible equity/crypto daily bounds.
pub fn assemble_study_frames(
    configuration: &BacktestConfig,
    fixture: &ReplayFixture,
    options: &SyntheticDataOptions,
) -> Result<Vec<EngineBarFrame>, BacktestError> {
    let bars = fixture
        .events
        .iter()
        .map(|event| market_event(event, options))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .filter_map(|event| match event {
            EngineMarket::Bar(bar) => Some(bar),
            _ => None,
        })
        .collect();
    assemble_bar_observations(configuration, bars)
}

/// Canonical session-anchored aggregation shared by replay and screening.
pub fn assemble_bar_observations(
    configuration: &BacktestConfig,
    mut bars: Vec<EngineBar>,
) -> Result<Vec<EngineBarFrame>, BacktestError> {
    bars.sort_by_key(|bar| (bar.timestamp_ns, bar.sequence));
    let invalid = |message: String| BacktestError::DataPreflight(message);
    let duration = replay_interval_ns(&configuration.data_bar_interval)?;
    let daily = duration.is_none();
    let source_daily = configuration.data_source_bar_interval == "1d";
    if source_daily && !daily {
        return Err(invalid("intraday frames require minute source bars".into()));
    }
    let symbols: BTreeSet<String> = configuration.symbols.iter().cloned().collect();
    let has_crypto = symbols
        .iter()
        .any(|symbol| marketdata::crypto_pair(symbol).is_some());
    let has_equity = symbols
        .iter()
        .any(|symbol| marketdata::crypto_pair(symbol).is_none());
    if daily && has_crypto && has_equity {
        return Err(invalid(
            "mixed equity/crypto DAILY study has incompatible interval bounds".to_string(),
        ));
    }
    let mut frames: BTreeMap<i64, EngineBarFrame> = BTreeMap::new();
    let mut seen = BTreeSet::new();
    let mut counts: BTreeMap<(i64, String), usize> = BTreeMap::new();
    for bar in bars {
        if !symbols.contains(&bar.symbol) {
            return Err(invalid(format!("unexpected study symbol {:?}", bar.symbol)));
        }
        let source_start = bar.timestamp_ns;
        let start = source_start;
        if !seen.insert((start, bar.symbol.clone())) {
            return Err(invalid(format!(
                "duplicate study timestamp/symbol: {start}/{}",
                bar.symbol
            )));
        }
        let date = marketdata::Date::from_days(
            start.div_euclid(harness_trading::replay_errors::ONE_DAY_NS) as i32,
        );
        let asset = if marketdata::crypto_pair(&bar.symbol).is_some() {
            marketdata::AssetClass::Crypto
        } else {
            marketdata::AssetClass::Equity
        };
        let sessions = marketdata::sessions_for(asset, date, date)
            .map_err(|error| invalid(error.to_string()))?;
        let session = sessions
            .first()
            .ok_or_else(|| invalid(format!("study bar {start} is outside a session")))?;
        let open = session.open_time.to_nanos();
        let close = session.close_time.to_nanos();
        let minute = 60_000_000_000;
        if start < open
            || start >= close
            || (source_daily && start != open)
            || (!daily && (start - open) % minute != 0)
        {
            return Err(invalid(format!(
                "misaligned study bar grid at {start} for {}",
                bar.symbol
            )));
        }
        let start = duration.map_or(open, |width| open + (source_start - open) / width * width);
        let end = duration.map_or(close, |width| start.saturating_add(width).min(close));
        let frame = frames.entry(start).or_insert_with(|| EngineBarFrame {
            interval: configuration.data_bar_interval.clone(),
            interval_start_ns: start,
            interval_end_ns: end,
            available_at_ns: end,
            bars_by_symbol: BTreeMap::new(),
            missing_symbols: Vec::new(),
        });
        if frame.interval_end_ns != end {
            return Err(invalid(format!(
                "incompatible study interval bounds at {start}"
            )));
        }
        *counts.entry((start, bar.symbol.clone())).or_default() += 1;
        frame
            .bars_by_symbol
            .entry(bar.symbol.clone())
            .and_modify(|aggregate| {
                aggregate.high = aggregate.high.max(bar.high);
                aggregate.low = aggregate.low.min(bar.low);
                aggregate.close = bar.close;
                aggregate.volume += bar.volume;
            })
            .or_insert_with(|| {
                let mut bar = bar;
                bar.timestamp_ns = start;
                bar
            });
    }
    Ok(frames
        .into_values()
        .filter_map(|mut frame| {
            if frame.interval_start_ns < configuration.start_ns
                || frame.interval_start_ns >= configuration.end_ns
                || frame.available_at_ns > configuration.end_ns
            {
                return None;
            }
            let expected = if source_daily {
                1
            } else {
                ((frame.interval_end_ns - frame.interval_start_ns) / 60_000_000_000) as usize
            };
            frame.bars_by_symbol.retain(|symbol, _| {
                counts
                    .get(&(frame.interval_start_ns, symbol.clone()))
                    .copied()
                    == Some(expected)
            });
            frame.missing_symbols = symbols
                .iter()
                .filter(|symbol| !frame.bars_by_symbol.contains_key(*symbol))
                .cloned()
                .collect();
            Some(frame)
        })
        .collect())
}

use super::projection::market_event;
