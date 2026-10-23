//! Converts replay source events into program-facing observations; no acquisition.
use crate::backtest::fixture::MarketEvent;
use crate::backtest::fixture::SyntheticDataOptions;
use harness_trading::program::*;
use harness_trading::replay_errors::BacktestError;
use num::Decimal;

/// Convert one fixture event to a Python-scale market event.
///
/// # Errors
///
/// Returns [`BacktestError::DataPreflight`] when a market value cannot be
/// represented as an engine decimal. Such a value never becomes zero.
pub fn market_event(
    event: &MarketEvent,
    options: &SyntheticDataOptions,
) -> Result<EngineMarket, BacktestError> {
    let (quote_scale, trade_scale) = crate::backtest::fixture::synthetic_scales(options);
    match event {
        MarketEvent::Quote {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            bid,
            ask,
            bid_quantity,
            ask_quantity,
            venue,
        } => Ok(EngineMarket::Quote {
            event_id: event_id.clone(),
            timestamp_ns: *timestamp_ns,
            sequence: *sequence,
            symbol: symbol.clone(),
            bid: py_from(*bid, quote_scale, "bid", symbol)?,
            ask: py_from(*ask, quote_scale, "ask", symbol)?,
            bid_quantity: py_from(*bid_quantity, options.quantity_scale, "bid size", symbol)?,
            ask_quantity: py_from(*ask_quantity, options.quantity_scale, "ask size", symbol)?,
            venue: venue.clone(),
        }),
        MarketEvent::Trade {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            price,
            quantity,
            venue,
            trade_id,
        } => Ok(EngineMarket::Trade {
            event_id: event_id.clone(),
            timestamp_ns: *timestamp_ns,
            sequence: *sequence,
            symbol: symbol.clone(),
            price: py_from(*price, trade_scale, "trade price", symbol)?,
            quantity: py_from(*quantity, options.quantity_scale, "trade size", symbol)?,
            venue: venue.clone(),
            trade_id: trade_id.clone(),
        }),
        MarketEvent::Bar {
            event_id,
            timestamp_ns,
            sequence,
            symbol,
            open,
            high,
            low,
            close,
            volume,
        } => Ok(EngineMarket::Bar(EngineBar {
            event_id: event_id.clone(),
            timestamp_ns: *timestamp_ns,
            sequence: *sequence,
            symbol: symbol.clone(),
            open: py_from(*open, quote_scale, "bar open", symbol)?,
            high: py_from(*high, quote_scale, "bar high", symbol)?,
            low: py_from(*low, quote_scale, "bar low", symbol)?,
            close: py_from(*close, quote_scale, "bar close", symbol)?,
            volume: py_from(*volume, options.quantity_scale, "bar volume", symbol)?,
        })),
    }
}

/// Re-read one exact fixture value as an engine decimal at its Python scale.
fn py_from(
    value: num::Decimal18,
    scale: u32,
    field: &str,
    symbol: &str,
) -> Result<Decimal, BacktestError> {
    let rounded = value
        .round_to(num::RoundingMode::HalfEven, scale)
        .map_err(|error| {
            BacktestError::DataPreflight(format!(
                "market {field} for symbol {symbol:?} cannot be rounded: {error}"
            ))
        })?;
    let power = num::SCALE
        .checked_sub(scale)
        .ok_or_else(|| BacktestError::DataPreflight("market scale exceeds 18".into()))?;
    Ok(Decimal::from_parts(
        rounded.is_negative(),
        rounded.unscaled().unsigned_abs() / 10u128.pow(power),
        scale as i32,
    ))
}
