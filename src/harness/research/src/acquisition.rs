//! Owns option resolution, market acquisition and pinned replay input planning.
//! Lake capabilities supply data; simulation policy and artifact writes belong elsewhere.
use std::collections::BTreeMap;

use harness_contracts::progress::ExecutionProgress;
use harness_contracts::progress::ExecutionStage;
use harness_lakeclient::BlockingLakeClient;
use harness_lakeclient::EnsureSpec;
use harness_lakeclient::ReadSpec;

use marketdata::DataType;
use marketdata::Date;

use crate::error::BacktestJobError;
use backtest::backtest::config::BacktestConfig;
use backtest::backtest::fixture::MarketEvent;
use backtest::backtest::fixture::ReplayFixture;
use backtest::backtest::fixture::SyntheticDataOptions;
use backtest::backtest::subscriptions::normalize_subscriptions;
use harness_trading::program::MarketSubscription;
use harness_trading::replay_errors::BacktestError;
use harness_trading::replay_errors::ONE_DAY_NS;

/// Full-precision scale options for materialized lake data.
///
/// Lake decimals are stored at scale 18; carrying the same scale through the
/// Python-compatible conversion preserves every digit, unlike the 2-digit
/// synthetic benchmark scales.
pub(super) fn lake_scale_options() -> SyntheticDataOptions {
    SyntheticDataOptions {
        base_price_scale: num::SCALE,
        tick_scale: num::SCALE,
        quantity_scale: num::SCALE,
        ..SyntheticDataOptions::default()
    }
}

/// Build a replay fixture from the market-lake read route.
///
/// Uses the declared configuration subscriptions. Backtests also acquire
/// separate quote/trade execution feeds. Equity symbols use the `equity` asset
/// class; symbols shaped like a crypto pair (`BTC-USD`) use the `crypto` asset
/// class with their canonical `BASE/QUOTE` symbol. Fetched crypto events are
/// re-keyed to the configured dashed symbol so the fixture, preflight, and
/// research context all agree.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the lake cannot serve the configured
/// window or the route payload cannot be decoded. There is no synthetic
/// fallback.
pub fn lake_fixture(
    configuration: &BacktestConfig,
    client: &BlockingLakeClient,
) -> Result<ReplayFixture, BacktestJobError> {
    lake_fixture_with_subscriptions(configuration, client, &configuration.data_subscriptions)
}

/// Acquire exact consumer inputs plus separate backtest execution feeds.
///
/// # Errors
/// Rejects unsupported cadences and empty requested symbol/type coverage.
pub fn lake_fixture_with_subscriptions(
    configuration: &BacktestConfig,
    client: &BlockingLakeClient,
    subscriptions: &[MarketSubscription],
) -> Result<ReplayFixture, BacktestJobError> {
    let requirements = normalize_subscriptions(subscriptions)?;
    if configuration.has_subscription_echo()
        && normalize_subscriptions(&configuration.data_subscriptions)? != requirements
    {
        return Err(BacktestJobError::Engine(
            "data.subscriptions does not match runtime subscription union".into(),
        ));
    }
    let mut acquisition = requirements.clone();
    if configuration.shell != "study" {
        acquisition.extend(backtest::backtest::subscriptions::execution_subscriptions(
            &requirements,
        ));
    }
    let acquisition =
        backtest::backtest::subscriptions::source_subscriptions(&acquisition, "1min")?;
    let start_date = Date::from_days(configuration.start_ns.div_euclid(ONE_DAY_NS) as i32);
    let end_date = Date::from_days(
        configuration
            .end_ns
            .saturating_sub(1)
            .div_euclid(ONE_DAY_NS) as i32,
    );
    let start_text = start_date.isoformat();
    let end_text = end_date.isoformat();
    let mut resolved = BTreeMap::new();
    for (alias, selector) in &configuration.option_selectors {
        let as_of = selector
            .validate()
            .map_err(|e| BacktestJobError::Io(e.to_string()))?;
        if as_of.to_nanos_i64() > configuration.start_ns {
            return Err(BacktestJobError::Io(
                "option selection must not follow replay start".into(),
            ));
        }
        if !configuration.symbols.contains(alias) {
            return Err(BacktestJobError::Io(
                "option input alias must be a configured symbol".into(),
            ));
        }
        resolved.insert(
            alias.clone(),
            client
                .resolve_option(selector)
                .map_err(|e| BacktestJobError::Io(e.to_string()))?,
        );
    }
    let (stream, events, sessions) = lake_subscription_events(
        configuration,
        client,
        &start_text,
        &end_text,
        &acquisition,
        &resolved,
    )?;
    if stream.evidence.event_count == 0 {
        return Err(BacktestJobError::Io(
            "the market lake served no requested events for the configured window".into(),
        ));
    }
    let fingerprints = stream.evidence.fingerprints.clone();
    let fixture = ReplayFixture {
        sessions,
        events,
        stream: Some(stream.clone()),
        source_id: stream.evidence.source_id.clone(),
        symbols: configuration.symbols.clone(),
        fingerprints,
        expected_data_fingerprint: None,
    };
    backtest::backtest::preflight::validate_subscription_coverage(&fixture, &requirements)?;
    let mut frame_config = configuration.clone();
    frame_config.data_source_bar_interval = "1min".into();
    let frames = backtest::backtest::subscriptions::assemble_subscription_frames(
        &frame_config,
        &fixture,
        &lake_scale_options(),
        &requirements,
    )?;
    backtest::backtest::preflight::validate_completed_bar_coverage(&requirements, &frames)?;
    Ok(fixture)
}

/// Ensure and read every normalized symbol/type requirement.
///
/// Input windows are ensured concurrently. Arrow batches are then decoded and
/// consumed incrementally, releasing each batch before the next arrives. Completion order cannot change the
/// replay: [`lake_fixture`] sorts the merged events by timestamp before any
/// preflight or engine work.
pub(super) fn lake_subscription_events(
    configuration: &BacktestConfig,
    client: &BlockingLakeClient,
    start_text: &str,
    end_text: &str,
    subscriptions: &[MarketSubscription],
    resolved: &BTreeMap<String, harness_contracts::ResolvedOption>,
) -> Result<backtest::backtest::stream::PlannedReplay, BacktestJobError> {
    let mut windows = Vec::new();
    for input in subscriptions {
        let data_type = match input.data_type.as_str() {
            "bars" => DataType::Bars,
            "quote" => DataType::Quote,
            "trade" => DataType::Trade,
            other => {
                return Err(BacktestJobError::from(BacktestError::Configuration(
                    format!("unknown data type {other:?}"),
                )))
            }
        };
        for symbol in &input.symbols {
            let (asset_class, lake_symbol) = if let Some(contract) = resolved.get(symbol) {
                ("option", contract.symbol.clone())
            } else if marketdata::lake::canonical_osi_symbol(symbol).is_ok() {
                ("option", symbol.trim().to_uppercase())
            } else {
                match marketdata::crypto_pair(symbol) {
                    Some(pair) => ("crypto", pair),
                    None => ("equity", symbol.trim().to_uppercase()),
                }
            };
            let mut ensure = EnsureSpec::new(
                asset_class,
                data_type.value(),
                &lake_symbol,
                start_text,
                end_text,
            );
            let mut read = ReadSpec::new(
                asset_class,
                data_type.value(),
                &lake_symbol,
                start_text,
                end_text,
            );
            if let Some(interval) = &input.interval {
                ensure = ensure.with_interval(interval);
                read = read.with_interval(interval);
            }
            windows.push((symbol.clone(), lake_symbol, data_type, ensure, read));
        }
    }
    let ensures: Vec<EnsureSpec> = windows
        .iter()
        .map(|(_, _, _, ensure, _)| ensure.clone())
        .collect();
    for (_, outcome) in windows.iter().zip(client.ensure_many(&ensures)) {
        outcome.map_err(|error| BacktestJobError::Io(error.to_string()))?;
    }
    client
        .report_progress(ExecutionProgress::stage(ExecutionStage::ReadingData))
        .map_err(|e| BacktestJobError::Io(e.to_string()))?;
    let cursor = std::sync::Arc::new(
        client
            .replay_cursor()
            .map_err(|e| BacktestJobError::Io(e.to_string()))?,
    );
    let inputs = windows
        .into_iter()
        .map(
            |(symbol, lake_symbol, data_type, _, read)| crate::market_replay::LakeInput {
                symbol,
                lake_symbol,
                data_type,
                read,
            },
        )
        .collect();
    let source_id = if resolved.is_empty() {
        format!("lake:{}", client.base_url())
    } else {
        format!(
            "lake:{};options={}",
            client.base_url(),
            serde_json::to_string(resolved).map_err(|e| BacktestJobError::Io(e.to_string()))?
        )
    };
    crate::market_replay::catalog(
        cursor,
        inputs,
        source_id,
        configuration.start_ns,
        configuration.end_ns,
        crate::history::paced(client.progress_reporter()),
    )
    .map_err(BacktestJobError::from)
}

/// Convert one normalized lake event into the engine's fixture event.
pub(super) fn lake_event(event: marketdata::MarketEvent, symbol: &str) -> MarketEvent {
    match event {
        marketdata::MarketEvent::Quote(quote) => MarketEvent::Quote {
            event_id: quote.event_id,
            timestamp_ns: quote.timestamp.to_nanos(),
            sequence: quote.sequence,
            symbol: symbol.to_string(),
            bid: quote.bid_price,
            ask: quote.ask_price,
            bid_quantity: quote.bid_size,
            ask_quantity: quote.ask_size,
            venue: quote.venue.unwrap_or_default(),
        },
        marketdata::MarketEvent::Trade(trade) => MarketEvent::Trade {
            event_id: trade.event_id,
            timestamp_ns: trade.timestamp.to_nanos(),
            sequence: trade.sequence,
            symbol: symbol.to_string(),
            price: trade.price,
            quantity: trade.quantity,
            venue: trade.venue.unwrap_or_default(),
            trade_id: trade.trade_id.unwrap_or_default(),
        },
        marketdata::MarketEvent::Bar(bar) => MarketEvent::Bar {
            event_id: bar.event_id,
            timestamp_ns: bar.timestamp.to_nanos(),
            sequence: bar.sequence,
            symbol: symbol.to_string(),
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
            volume: bar.volume,
        },
    }
}
