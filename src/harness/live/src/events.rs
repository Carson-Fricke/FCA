//! Immutable market-data event records.
//!
//! Ports the deterministic market payloads of `harness.core.types.events`
//! (`QuoteEvent`, `TradeEvent`). The event envelope (`event_id`, `timestamp`,
//! `sequence`) is owned by the async runtime and intentionally omitted from the
//! synchronous kernel.

use num::Decimal18;

use crate::error::{require_text, LiveError, LiveResult};
use crate::symbol::Symbol;

/// A top-of-book quote with exact price and quantity values.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct QuoteEvent {
    /// Canonical symbol.
    pub symbol: Symbol,
    /// Best bid price.
    pub bid_price: Decimal18,
    /// Size displayed at the best bid.
    pub bid_quantity: Decimal18,
    /// Best ask price.
    pub ask_price: Decimal18,
    /// Size displayed at the best ask.
    pub ask_quantity: Decimal18,
    /// Optional venue tag.
    pub venue: Option<String>,
}

impl QuoteEvent {
    /// Construct a validated quote.
    pub fn new(
        symbol: Symbol,
        bid_price: Decimal18,
        bid_quantity: Decimal18,
        ask_price: Decimal18,
        ask_quantity: Decimal18,
        venue: Option<String>,
    ) -> LiveResult<Self> {
        for (value, field) in [
            (bid_price, "bid_price"),
            (bid_quantity, "bid_quantity"),
            (ask_price, "ask_price"),
            (ask_quantity, "ask_quantity"),
        ] {
            if !value.is_positive() {
                return Err(LiveError::Validation(format!("{field} must be positive")));
            }
        }
        let venue = match venue {
            Some(value) => Some(require_text(&value, "venue")?),
            None => None,
        };
        Ok(Self {
            symbol,
            bid_price,
            bid_quantity,
            ask_price,
            ask_quantity,
            venue,
        })
    }
}

/// A market trade with exact price and quantity values.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TradeEvent {
    /// Canonical symbol.
    pub symbol: Symbol,
    /// Trade price.
    pub price: Decimal18,
    /// Trade quantity.
    pub quantity: Decimal18,
    /// Optional venue tag.
    pub venue: Option<String>,
    /// Optional trade identifier.
    pub trade_id: Option<String>,
}

impl TradeEvent {
    /// Construct a validated trade.
    pub fn new(
        symbol: Symbol,
        price: Decimal18,
        quantity: Decimal18,
        venue: Option<String>,
        trade_id: Option<String>,
    ) -> LiveResult<Self> {
        for (value, field) in [(price, "price"), (quantity, "quantity")] {
            if !value.is_positive() {
                return Err(LiveError::Validation(format!("{field} must be positive")));
            }
        }
        let venue = match venue {
            Some(value) => Some(require_text(&value, "venue")?),
            None => None,
        };
        let trade_id = match trade_id {
            Some(value) => Some(require_text(&value, "trade_id")?),
            None => None,
        };
        Ok(Self {
            symbol,
            price,
            quantity,
            venue,
            trade_id,
        })
    }
}

/// A market snapshot observed by a fill model.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MarketEvent {
    /// A top-of-book quote.
    Quote(QuoteEvent),
    /// A trade print.
    Trade(TradeEvent),
}

impl MarketEvent {
    /// The snapshot's canonical symbol.
    pub fn symbol(&self) -> &Symbol {
        match self {
            Self::Quote(quote) => &quote.symbol,
            Self::Trade(trade) => &trade.symbol,
        }
    }

    /// The quote payload, when this is a quote.
    pub fn as_quote(&self) -> Option<&QuoteEvent> {
        match self {
            Self::Quote(quote) => Some(quote),
            Self::Trade(_) => None,
        }
    }

    /// The trade payload, when this is a trade.
    pub fn as_trade(&self) -> Option<&TradeEvent> {
        match self {
            Self::Trade(trade) => Some(trade),
            Self::Quote(_) => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use num::Decimal18;

    use super::{MarketEvent, QuoteEvent};
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    #[test]
    fn quote_requires_positive_prices() {
        let symbol = Symbol::new("AAPL").unwrap();
        assert!(QuoteEvent::new(
            symbol.clone(),
            dec("100"),
            dec("1"),
            dec("101"),
            dec("1"),
            None
        )
        .is_ok());
        assert!(QuoteEvent::new(symbol, dec("0"), dec("1"), dec("101"), dec("1"), None).is_err());
    }

    #[test]
    fn market_event_exposes_symbol() {
        let event = MarketEvent::Quote(
            QuoteEvent::new(
                Symbol::new("AAPL").unwrap(),
                dec("100"),
                dec("1"),
                dec("101"),
                dec("1"),
                None,
            )
            .unwrap(),
        );
        assert_eq!(event.symbol().as_str(), "AAPL");
        assert!(event.as_quote().is_some());
        assert!(event.as_trade().is_none());
    }
}
