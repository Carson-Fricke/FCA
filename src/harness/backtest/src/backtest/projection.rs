//! Converts replay source events into program-facing observations; no acquisition.
use crate::backtest::fixture::MarketEvent;
use crate::backtest::fixture::SyntheticDataOptions;
use harness_trading::program::*;
use harness_trading::replay_errors::BacktestError;
use num::Decimal;

/// Run-scoped conversion settings. Owns exact numeric projection and moves event
/// identifiers into execution; acquisition and replay ordering remain separate.
#[derive(Clone, Copy, Debug)]
pub struct EventProjection {
    quote_scale: u32,
    trade_scale: u32,
    quantity_scale: u32,
}

impl EventProjection {
    pub fn new(options: &SyntheticDataOptions) -> Self {
        let (quote_scale, trade_scale) = crate::backtest::fixture::synthetic_scales(options);
        Self {
            quote_scale,
            trade_scale,
            quantity_scale: options.quantity_scale,
        }
    }

    /// Consume an event without cloning its owned identifiers. Unrepresentable
    /// numeric values fail rather than becoming zero.
    pub fn project(&self, event: MarketEvent) -> Result<EngineMarket, BacktestError> {
        let Self {
            quote_scale,
            trade_scale,
            quantity_scale,
        } = *self;
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
                event_id,
                timestamp_ns,
                sequence,
                bid: py_from(bid, quote_scale, "bid", &symbol)?,
                ask: py_from(ask, quote_scale, "ask", &symbol)?,
                bid_quantity: py_from(bid_quantity, quantity_scale, "bid size", &symbol)?,
                ask_quantity: py_from(ask_quantity, quantity_scale, "ask size", &symbol)?,
                venue,
                symbol,
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
                event_id,
                timestamp_ns,
                sequence,
                price: py_from(price, trade_scale, "trade price", &symbol)?,
                quantity: py_from(quantity, quantity_scale, "trade size", &symbol)?,
                venue,
                trade_id,
                symbol,
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
                event_id,
                timestamp_ns,
                sequence,
                open: py_from(open, quote_scale, "bar open", &symbol)?,
                high: py_from(high, quote_scale, "bar high", &symbol)?,
                low: py_from(low, quote_scale, "bar low", &symbol)?,
                close: py_from(close, quote_scale, "bar close", &symbol)?,
                volume: py_from(volume, quantity_scale, "bar volume", &symbol)?,
                symbol,
            })),
        }
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
