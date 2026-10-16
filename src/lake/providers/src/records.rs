//! Normalized provider records independent of HTTP transport.
//!
//! Exact records for equity, option and crypto acquisition. Ingestion owns
//! canonical ordering and direct Arrow projection; providers do not render
//! persistence rows or convert fixed-point values to intermediate text.

use lake_catalog::OptionType;
use lake_contracts::{Date, DateTime, Decimal18};

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
    pub symbol: String,
    /// Canonical underlying symbol of the requested contract.
    pub underlying: String,
    /// Expiration date of the requested contract.
    pub expiration_date: Date,
    /// Strike price of the requested contract.
    pub strike_price: Decimal18,
    /// Call or put side of the requested contract.
    pub option_type: OptionType,
    /// Exchange code (`x`).
    pub exchange: String,
    /// Trade price (`p`).
    pub price: Decimal18,
    /// Contract size (`s`).
    pub size: u32,
    /// Sale condition (`c`): one string for options, not an array.
    pub condition: String,
}

/// One option quote for a selected OSI contract.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NormalizedOptionQuote {
    /// Provider timestamp in UTC.
    pub timestamp: DateTime,
    /// Canonical OSI symbol of the requested contract.
    pub symbol: String,
    /// Canonical underlying symbol of the requested contract.
    pub underlying: String,
    /// Expiration date of the requested contract.
    pub expiration_date: Date,
    /// Strike price of the requested contract.
    pub strike_price: Decimal18,
    /// Call or put side of the requested contract.
    pub option_type: OptionType,
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
    /// Quote condition (`c`): one string for options, not an array.
    pub condition: String,
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
    pub symbol: String,
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

impl Timestamped for NormalizedBar {
    fn timestamp(&self) -> DateTime {
        self.timestamp
    }
}
