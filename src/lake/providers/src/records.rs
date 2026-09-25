//! Normalized provider records independent of HTTP transport.
//!
//! Ports `lake.providers.alpaca.records` for the equity and crypto payloads
//! the Rust ingest path consumes. `to_row` renders the exact ingestion row the
//! Python writer produced: nanosecond integer timestamps, canonical decimal
//! strings, and the provider field names.

use lake_contracts::{DateTime, Decimal18};
use serde_json::{json, Value};

/// One equity trade.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedTrade {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical requested symbol.
    pub symbol: String,
    /// Provider trade identifier (`i`).
    pub trade_id: u64,
    /// Exchange code (`x`).
    pub exchange: String,
    /// Trade price (`p`).
    pub price: Decimal18,
    /// Share size (`s`).
    pub size: u32,
    /// Sale conditions (`c`).
    pub conditions: Vec<String>,
    /// Tape (`z`).
    pub tape: String,
    /// Update marker (`u`); absent when the provider omits it.
    pub update: Option<String>,
}

impl NormalizedTrade {
    /// The canonical ingestion row.
    pub fn to_row(&self) -> Value {
        json!({
            "timestamp": self.timestamp.to_nanos(),
            "symbol": self.symbol,
            "trade_id": self.trade_id,
            "exchange": self.exchange,
            "price": self.price.to_canonical_string(),
            "size": self.size,
            "conditions": self.conditions,
            "tape": self.tape,
            "update": self.update,
        })
    }
}

/// One equity quote.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedQuote {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical requested symbol.
    pub symbol: String,
    /// Bid exchange code (`bx`).
    pub bid_exchange: String,
    /// Bid price (`bp`).
    pub bid_price: Decimal18,
    /// Bid size (`bs`).
    pub bid_size: u32,
    /// Ask exchange code (`ax`).
    pub ask_exchange: String,
    /// Ask price (`ap`).
    pub ask_price: Decimal18,
    /// Ask size (`as`).
    pub ask_size: u32,
    /// Quote conditions (`c`).
    pub conditions: Vec<String>,
    /// Tape (`z`).
    pub tape: String,
}

impl NormalizedQuote {
    /// The canonical ingestion row.
    pub fn to_row(&self) -> Value {
        json!({
            "timestamp": self.timestamp.to_nanos(),
            "symbol": self.symbol,
            "bid_exchange": self.bid_exchange,
            "bid_price": self.bid_price.to_canonical_string(),
            "bid_size": self.bid_size,
            "ask_exchange": self.ask_exchange,
            "ask_price": self.ask_price.to_canonical_string(),
            "ask_size": self.ask_size,
            "conditions": self.conditions,
            "tape": self.tape,
        })
    }
}

/// One crypto trade.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedCryptoTrade {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical requested pair.
    pub symbol: String,
    /// Provider trade identifier (`i`); absent when the provider omits it.
    pub trade_id: Option<i64>,
    /// Exchange code (`x`); absent when the provider omits it.
    pub exchange: Option<String>,
    /// Trade price (`p`).
    pub price: Decimal18,
    /// Trade size (`s`), fractional for crypto.
    pub size: Decimal18,
    /// Taker side (`tks`); absent when the provider omits it.
    pub taker_side: Option<String>,
}

impl NormalizedCryptoTrade {
    /// The canonical ingestion row.
    pub fn to_row(&self) -> Value {
        json!({
            "timestamp": self.timestamp.to_nanos(),
            "symbol": self.symbol,
            "trade_id": self.trade_id,
            "exchange": self.exchange,
            "price": self.price.to_canonical_string(),
            "size": self.size.to_canonical_string(),
            "taker_side": self.taker_side,
        })
    }
}

/// One crypto quote.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedCryptoQuote {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical requested pair.
    pub symbol: String,
    /// Bid exchange code (`bx`); absent when the provider omits it.
    pub bid_exchange: Option<String>,
    /// Bid price (`bp`).
    pub bid_price: Decimal18,
    /// Bid size (`bs`), fractional for crypto.
    pub bid_size: Decimal18,
    /// Ask exchange code (`ax`); absent when the provider omits it.
    pub ask_exchange: Option<String>,
    /// Ask price (`ap`).
    pub ask_price: Decimal18,
    /// Ask size (`as`), fractional for crypto.
    pub ask_size: Decimal18,
}

impl NormalizedCryptoQuote {
    /// The canonical ingestion row.
    pub fn to_row(&self) -> Value {
        json!({
            "timestamp": self.timestamp.to_nanos(),
            "symbol": self.symbol,
            "bid_exchange": self.bid_exchange,
            "bid_price": self.bid_price.to_canonical_string(),
            "bid_size": self.bid_size.to_canonical_string(),
            "ask_exchange": self.ask_exchange,
            "ask_price": self.ask_price.to_canonical_string(),
            "ask_size": self.ask_size.to_canonical_string(),
        })
    }
}

/// The parseable record timestamp used by the time-sliced planner.
pub(crate) trait Timestamped {
    /// The record's UTC timestamp.
    fn timestamp(&self) -> DateTime;
}

impl Timestamped for NormalizedTrade {
    fn timestamp(&self) -> DateTime {
        self.timestamp
    }
}

impl Timestamped for NormalizedQuote {
    fn timestamp(&self) -> DateTime {
        self.timestamp
    }
}

impl Timestamped for NormalizedCryptoTrade {
    fn timestamp(&self) -> DateTime {
        self.timestamp
    }
}

impl Timestamped for NormalizedCryptoQuote {
    fn timestamp(&self) -> DateTime {
        self.timestamp
    }
}

#[cfg(test)]
#[path = "../tests/unit/records.rs"]
mod tests;
