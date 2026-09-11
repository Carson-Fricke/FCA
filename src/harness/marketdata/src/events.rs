//! Deterministic market-event value objects.
//!
//! The history and replay layers exchange quote and trade events. The Rust
//! port keeps the exact `(timestamp, sequence, event-class priority)` ordering
//! key the Python event bus uses so replay streams and retained history sort
//! identically.

use num::Decimal18;

use crate::time::{DateTime, Nanos};

/// Event classes recognized by the deterministic bus, in priority order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum EventType {
    /// A top-of-book quote.
    Quote,
    /// A trade print.
    Trade,
    /// A single-leg order.
    Order,
    /// A multi-leg order.
    MultiLegOrder,
    /// An execution report.
    ExecutionReport,
    /// A fill.
    Fill,
    /// A position update.
    Position,
    /// A book update.
    Book,
    /// An account update.
    Account,
}

impl EventType {
    /// The deterministic class priority used as the ordering-key tail.
    pub const fn priority(self) -> u8 {
        match self {
            Self::Quote => 0,
            Self::Trade => 1,
            Self::Order => 2,
            Self::MultiLegOrder => 3,
            Self::ExecutionReport => 4,
            Self::Fill => 5,
            Self::Position => 6,
            Self::Book => 7,
            Self::Account => 8,
        }
    }
}

/// A top-of-book quote.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct QuoteEvent {
    /// Stable event identity.
    pub event_id: String,
    /// UTC instant of the quote.
    pub timestamp: DateTime,
    /// Canonical symbol.
    pub symbol: String,
    /// Deterministic intra-timestamp sequence.
    pub sequence: u64,
    /// Best bid price.
    pub bid_price: Decimal18,
    /// Best ask price.
    pub ask_price: Decimal18,
    /// Best bid size.
    pub bid_size: Decimal18,
    /// Best ask size.
    pub ask_size: Decimal18,
    /// Provider venue, when one was supplied.
    pub venue: Option<String>,
}

/// A trade print.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TradeEvent {
    /// Stable event identity.
    pub event_id: String,
    /// UTC instant of the trade.
    pub timestamp: DateTime,
    /// Canonical symbol.
    pub symbol: String,
    /// Deterministic intra-timestamp sequence.
    pub sequence: u64,
    /// Execution price.
    pub price: Decimal18,
    /// Executed quantity.
    pub quantity: Decimal18,
    /// Provider venue, when one was supplied.
    pub venue: Option<String>,
    /// Provider trade identifier, when one was supplied.
    pub trade_id: Option<String>,
}

impl TradeEvent {
    /// Build a minimal trade event with a generated identity.
    pub fn new(
        symbol: String,
        timestamp: DateTime,
        sequence: u64,
        price: Decimal18,
        quantity: Decimal18,
    ) -> Self {
        Self {
            event_id: format!("{symbol}-{sequence}"),
            timestamp,
            symbol,
            sequence,
            price,
            quantity,
            venue: None,
            trade_id: None,
        }
    }
}

impl QuoteEvent {
    /// Build a minimal quote event with a generated identity.
    pub fn new(
        symbol: String,
        timestamp: DateTime,
        sequence: u64,
        bid_price: Decimal18,
        ask_price: Decimal18,
    ) -> Self {
        Self {
            event_id: format!("{symbol}-{sequence}"),
            timestamp,
            symbol,
            sequence,
            bid_price,
            ask_price,
            bid_size: Decimal18::ZERO,
            ask_size: Decimal18::ZERO,
            venue: None,
        }
    }
}

/// The ordering key shared by the event bus and retained history.
pub type OrderingKey = (Nanos, u64, u8);

/// The deterministic ordering key for one event.
pub fn ordering_key(timestamp: DateTime, sequence: u64, event_type: EventType) -> OrderingKey {
    (timestamp.to_nanos(), sequence, event_type.priority())
}

/// A quote-or-trade market event.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MarketEvent {
    /// A quote.
    Quote(QuoteEvent),
    /// A trade.
    Trade(TradeEvent),
}

impl MarketEvent {
    /// The event class.
    pub fn event_type(&self) -> EventType {
        match self {
            Self::Quote(_) => EventType::Quote,
            Self::Trade(_) => EventType::Trade,
        }
    }

    /// The event instant.
    pub fn timestamp(&self) -> DateTime {
        match self {
            Self::Quote(event) => event.timestamp,
            Self::Trade(event) => event.timestamp,
        }
    }

    /// The canonical symbol.
    pub fn symbol(&self) -> &str {
        match self {
            Self::Quote(event) => &event.symbol,
            Self::Trade(event) => &event.symbol,
        }
    }

    /// The intra-timestamp sequence.
    pub fn sequence(&self) -> u64 {
        match self {
            Self::Quote(event) => event.sequence,
            Self::Trade(event) => event.sequence,
        }
    }

    /// The stable event identity.
    pub fn event_id(&self) -> &str {
        match self {
            Self::Quote(event) => &event.event_id,
            Self::Trade(event) => &event.event_id,
        }
    }

    /// The deterministic ordering key.
    pub fn ordering_key(&self) -> OrderingKey {
        ordering_key(self.timestamp(), self.sequence(), self.event_type())
    }
}

/// Inclusive upper bound: every event at or before `as_of`.
pub const fn cutoff_key(as_of: Nanos) -> OrderingKey {
    (as_of, u64::MAX, u8::MAX)
}

/// Inclusive lower bound: every event at or after `start`.
pub const fn start_key(start: Nanos) -> OrderingKey {
    (start, 0, 0)
}

/// Exclusive upper bound: every event strictly before `end`.
pub const fn end_key(end: Nanos) -> OrderingKey {
    (end - 1, u64::MAX, u8::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn priorities_match_python_bus() {
        assert_eq!(EventType::Quote.priority(), 0);
        assert_eq!(EventType::Trade.priority(), 1);
        assert_eq!(EventType::Account.priority(), 8);
    }
}
