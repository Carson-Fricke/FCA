//! Normalized provider records independent of HTTP transport.
//!
//! Exact records for equity, option and crypto acquisition. Ingestion owns
//! canonical ordering and direct Arrow projection; providers do not render
//! persistence rows or convert fixed-point values to intermediate text.

use lake_catalog::OptionType;
use lake_contracts::{Date, DateTime, Decimal18};
use smol_str::SmolStr;

/// One equity trade.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedTrade {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical requested symbol.
    pub symbol: SmolStr,
    /// Provider trade identifier (`i`).
    pub trade_id: u64,
    /// Exchange code (`x`).
    pub exchange: SmolStr,
    /// Trade price (`p`).
    pub price: Decimal18,
    /// Share size (`s`).
    pub size: u32,
    /// Sale conditions (`c`).
    pub conditions: Vec<SmolStr>,
    /// Tape (`z`).
    pub tape: SmolStr,
    /// Update marker (`u`); absent when the provider omits it.
    pub update: Option<SmolStr>,
}

/// One equity quote.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedQuote {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical requested symbol.
    pub symbol: SmolStr,
    /// Bid exchange code (`bx`).
    pub bid_exchange: SmolStr,
    /// Bid price (`bp`).
    pub bid_price: Decimal18,
    /// Bid size (`bs`).
    pub bid_size: u32,
    /// Ask exchange code (`ax`).
    pub ask_exchange: SmolStr,
    /// Ask price (`ap`).
    pub ask_price: Decimal18,
    /// Ask size (`as`).
    pub ask_size: u32,
    /// Quote conditions (`c`).
    pub conditions: Vec<SmolStr>,
    /// Tape (`z`).
    pub tape: SmolStr,
}

/// One option trade for a selected OSI contract.
///
/// The contract identity fields (`symbol`, `underlying`, `expiration_date`,
/// `strike_price`, `option_type`) come from the requested contract, exactly as
/// the Python client derives them; only the OPRA fields are read from the row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedOptionTrade {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical OSI symbol of the requested contract.
    pub symbol: SmolStr,
    /// Canonical underlying symbol of the requested contract.
    pub underlying: SmolStr,
    /// Expiration date of the requested contract.
    pub expiration_date: Date,
    /// Strike price of the requested contract.
    pub strike_price: Decimal18,
    /// Call or put side of the requested contract.
    pub option_type: OptionType,
    /// Exchange code (`x`).
    pub exchange: SmolStr,
    /// Trade price (`p`).
    pub price: Decimal18,
    /// Contract size (`s`).
    pub size: u32,
    /// Sale condition (`c`): one string for options, not an array.
    pub condition: SmolStr,
}

/// One option quote for a selected OSI contract.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedOptionQuote {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical OSI symbol of the requested contract.
    pub symbol: SmolStr,
    /// Canonical underlying symbol of the requested contract.
    pub underlying: SmolStr,
    /// Expiration date of the requested contract.
    pub expiration_date: Date,
    /// Strike price of the requested contract.
    pub strike_price: Decimal18,
    /// Call or put side of the requested contract.
    pub option_type: OptionType,
    /// Bid exchange code (`bx`).
    pub bid_exchange: SmolStr,
    /// Bid price (`bp`).
    pub bid_price: Decimal18,
    /// Bid size (`bs`).
    pub bid_size: u32,
    /// Ask exchange code (`ax`).
    pub ask_exchange: SmolStr,
    /// Ask price (`ap`).
    pub ask_price: Decimal18,
    /// Ask size (`as`).
    pub ask_size: u32,
    /// Quote condition (`c`): one string for options, not an array.
    pub condition: SmolStr,
}

/// One crypto trade.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedCryptoTrade {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical requested pair.
    pub symbol: SmolStr,
    /// Provider trade identifier (`i`); absent when the provider omits it.
    pub trade_id: Option<i64>,
    /// Exchange code (`x`); absent when the provider omits it.
    pub exchange: Option<SmolStr>,
    /// Trade price (`p`).
    pub price: Decimal18,
    /// Trade size (`s`), fractional for crypto.
    pub size: Decimal18,
    /// Taker side (`tks`); absent when the provider omits it.
    pub taker_side: Option<SmolStr>,
}

/// One crypto quote.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedCryptoQuote {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical requested pair.
    pub symbol: SmolStr,
    /// Bid exchange code (`bx`); absent when the provider omits it.
    pub bid_exchange: Option<SmolStr>,
    /// Bid price (`bp`).
    pub bid_price: Decimal18,
    /// Bid size (`bs`), fractional for crypto.
    pub bid_size: Decimal18,
    /// Ask exchange code (`ax`); absent when the provider omits it.
    pub ask_exchange: Option<SmolStr>,
    /// Ask price (`ap`).
    pub ask_price: Decimal18,
    /// Ask size (`as`), fractional for crypto.
    pub ask_size: Decimal18,
}

/// One session-aligned OHLCV bar.
///
/// The lake convention fixes the row timestamp to the bucket start in UTC:
/// the exchange session open for daily bars and the session-open-aligned
/// minute window start for 1-minute bars. Every value is an exact decimal.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedBar {
    /// Bucket start in UTC.
    pub timestamp: DateTime,
    /// Canonical requested symbol.
    pub symbol: SmolStr,
    /// Exact open price.
    pub open: Decimal18,
    /// Exact high price.
    pub high: Decimal18,
    /// Exact low price.
    pub low: Decimal18,
    /// Exact close price.
    pub close: Decimal18,
    /// Exact volume.
    pub volume: Decimal18,
}

/// The parseable record timestamp used by the time-sliced planner.
pub(crate) trait Timestamped: RecordMemory {
    /// The record's UTC timestamp.
    fn timestamp(&self) -> DateTime;
}

/// Retained normalized allocation accounting; used before appending another page.
pub trait RecordMemory {
    fn allocated_bytes(&self) -> usize;
}

fn heap_bytes(value: &SmolStr) -> usize {
    if value.is_heap_allocated() {
        value.len()
    } else {
        0
    }
}
fn text(value: &Option<SmolStr>) -> usize {
    value.as_ref().map_or(0, heap_bytes)
}
fn conditions(values: &Vec<SmolStr>) -> usize {
    values.capacity() * std::mem::size_of::<SmolStr>()
        + values.iter().map(heap_bytes).sum::<usize>()
}
impl RecordMemory for NormalizedTrade {
    fn allocated_bytes(&self) -> usize {
        std::mem::size_of::<Self>()
            + heap_bytes(&self.symbol)
            + heap_bytes(&self.exchange)
            + heap_bytes(&self.tape)
            + text(&self.update)
            + conditions(&self.conditions)
    }
}
impl RecordMemory for NormalizedQuote {
    fn allocated_bytes(&self) -> usize {
        std::mem::size_of::<Self>()
            + heap_bytes(&self.symbol)
            + heap_bytes(&self.bid_exchange)
            + heap_bytes(&self.ask_exchange)
            + heap_bytes(&self.tape)
            + conditions(&self.conditions)
    }
}
impl RecordMemory for NormalizedCryptoTrade {
    fn allocated_bytes(&self) -> usize {
        std::mem::size_of::<Self>()
            + heap_bytes(&self.symbol)
            + text(&self.exchange)
            + text(&self.taker_side)
    }
}
impl RecordMemory for NormalizedCryptoQuote {
    fn allocated_bytes(&self) -> usize {
        std::mem::size_of::<Self>()
            + heap_bytes(&self.symbol)
            + text(&self.bid_exchange)
            + text(&self.ask_exchange)
    }
}
impl RecordMemory for NormalizedBar {
    fn allocated_bytes(&self) -> usize {
        std::mem::size_of::<Self>() + heap_bytes(&self.symbol)
    }
}
impl RecordMemory for NormalizedOptionTrade {
    fn allocated_bytes(&self) -> usize {
        std::mem::size_of::<Self>()
            + heap_bytes(&self.symbol)
            + heap_bytes(&self.underlying)
            + heap_bytes(&self.exchange)
            + heap_bytes(&self.condition)
    }
}
impl RecordMemory for NormalizedOptionQuote {
    fn allocated_bytes(&self) -> usize {
        std::mem::size_of::<Self>()
            + heap_bytes(&self.symbol)
            + heap_bytes(&self.underlying)
            + heap_bytes(&self.bid_exchange)
            + heap_bytes(&self.ask_exchange)
            + heap_bytes(&self.condition)
    }
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

impl Timestamped for NormalizedBar {
    fn timestamp(&self) -> DateTime {
        self.timestamp
    }
}
