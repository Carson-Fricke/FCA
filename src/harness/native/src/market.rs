//! Market-data and order domain records for the native replay/venue modules.
//!
//! Ports the deterministic market records from `src/harness/core/types`
//! (`QuoteEvent`/`TradeEvent`) and the price-bearing single-leg order
//! (`SingleLegOrder`) used by `src/harness/fills` and
//! `src/harness/venues/sim.py`.
//!
//! Validation mirrors the Python `__post_init__` contracts: identifiers and
//! symbols are non-empty, symbols must already be canonical, prices and
//! quantities are strictly positive, and order types carry exactly the
//! limit/stop prices their type allows.

use crate::types::{canonical_symbol, TypeError};

/// Serialized event classes, mirroring `harness.core.types.events.EventType`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum EventType {
    /// Top-of-book quote.
    Quote,
    /// Market trade.
    Trade,
    /// Single-leg order.
    Order,
    /// Multi-leg order.
    MultiLegOrder,
    /// Execution report.
    ExecutionReport,
    /// Fill.
    Fill,
    /// Position snapshot.
    Position,
    /// Book snapshot.
    Book,
    /// Account snapshot.
    Account,
}

/// The public ordering contract, mirrored from
/// `harness.core.events.EVENT_CLASS_PRIORITY`.
pub const EVENT_CLASS_PRIORITY: [u8; 9] = [0, 1, 2, 3, 4, 5, 6, 7, 8];

impl EventType {
    /// Serialized value used in the dispatch identity (`EventType.value`).
    pub const fn value(self) -> &'static str {
        match self {
            Self::Quote => "quote",
            Self::Trade => "trade",
            Self::Order => "order",
            Self::MultiLegOrder => "multi_leg_order",
            Self::ExecutionReport => "execution_report",
            Self::Fill => "fill",
            Self::Position => "position",
            Self::Book => "book",
            Self::Account => "account",
        }
    }

    /// Default Python-qualified class name used in the dispatch identity.
    pub const fn default_type_name(self) -> &'static str {
        match self {
            Self::Quote => "harness.core.types.events.QuoteEvent",
            Self::Trade => "harness.core.types.events.TradeEvent",
            Self::Order => "harness.core.types.orders.SingleLegOrder",
            Self::MultiLegOrder => "harness.core.types.orders.MultiLegOrder",
            Self::ExecutionReport => "harness.core.types.accounting.ExecutionReport",
            Self::Fill => "harness.core.types.accounting.Fill",
            Self::Position => "harness.core.types.accounting.Position",
            Self::Book => "harness.core.types.accounting.Book",
            Self::Account => "harness.core.types.accounting.AccountSnapshot",
        }
    }

    /// Class priority from the shared ordering contract.
    pub const fn class_priority(self) -> u8 {
        EVENT_CLASS_PRIORITY[self as usize]
    }
}

/// Order side.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Side {
    /// Buy.
    Buy,
    /// Sell.
    Sell,
}

/// Order type.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum OrderType {
    /// Market.
    Market,
    /// Limit.
    Limit,
    /// Stop.
    Stop,
    /// Stop-limit.
    StopLimit,
}

/// Time in force.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum TimeInForce {
    /// Day.
    Day,
    /// Good-till-canceled.
    Gtc,
    /// Immediate-or-cancel.
    Ioc,
}

/// A checked failure from the market/order domain.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// Allocation failure.
    OutOfMemory,
    /// A required argument was empty or otherwise invalid.
    InvalidArgument,
    /// The symbol is not canonical.
    InvalidSymbol,
    /// A decimal operation failed.
    InvalidDecimal,
    /// A decimal value was outside its range.
    OutOfRange,
}

impl core::fmt::Display for Error {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::OutOfMemory => "out of memory",
            Self::InvalidArgument => "invalid argument",
            Self::InvalidSymbol => "invalid symbol",
            Self::InvalidDecimal => "invalid decimal",
            Self::OutOfRange => "out of range",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for Error {}

fn require_canonical_symbol(symbol: &str) -> Result<(), Error> {
    if symbol.is_empty() {
        return Err(Error::InvalidArgument);
    }
    let bytes = symbol.as_bytes();
    let mut buffer = [0u8; 256];
    if bytes.len() > buffer.len() {
        return Err(Error::InvalidSymbol);
    }
    let (len, _) = canonical_symbol(bytes, &mut buffer).map_err(|error| match error {
        TypeError::BufferTooSmall => Error::InvalidSymbol,
        _ => Error::InvalidSymbol,
    })?;
    if &buffer[..len] != bytes {
        return Err(Error::InvalidSymbol);
    }
    Ok(())
}

fn require_text(value: &str) -> Result<(), Error> {
    if value.is_empty() {
        return Err(Error::InvalidArgument);
    }
    Ok(())
}

fn require_positive(value: i128) -> Result<(), Error> {
    if value <= 0 {
        return Err(Error::InvalidArgument);
    }
    Ok(())
}

/// A top-of-book quote (`QuoteEvent`). All four book values are required,
/// finite, and strictly positive.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct QuoteEvent<'a> {
    /// Event identifier.
    pub event_id: &'a str,
    /// Epoch nanoseconds.
    pub timestamp_ns: i128,
    /// Monotonic sequence number.
    pub sequence: u64,
    /// Canonical symbol.
    pub symbol: &'a str,
    /// Best bid price (unscaled scale 18).
    pub bid_price: i128,
    /// Best bid quantity (unscaled scale 18).
    pub bid_quantity: i128,
    /// Best ask price (unscaled scale 18).
    pub ask_price: i128,
    /// Best ask quantity (unscaled scale 18).
    pub ask_quantity: i128,
    /// Optional venue identifier.
    pub venue: Option<&'a str>,
}

impl<'a> QuoteEvent<'a> {
    /// Validate and return the quote.
    pub fn new(event: QuoteEvent<'a>) -> Result<Self, Error> {
        event.validate()?;
        Ok(event)
    }

    /// Validate the quote against the Python record contract.
    pub fn validate(&self) -> Result<(), Error> {
        require_text(self.event_id)?;
        require_canonical_symbol(self.symbol)?;
        require_positive(self.bid_price)?;
        require_positive(self.bid_quantity)?;
        require_positive(self.ask_price)?;
        require_positive(self.ask_quantity)?;
        if let Some(venue) = self.venue {
            require_text(venue)?;
        }
        Ok(())
    }
}

/// A market trade (`TradeEvent`) with exact price and quantity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TradeEvent<'a> {
    /// Event identifier.
    pub event_id: &'a str,
    /// Epoch nanoseconds.
    pub timestamp_ns: i128,
    /// Monotonic sequence number.
    pub sequence: u64,
    /// Canonical symbol.
    pub symbol: &'a str,
    /// Trade price (unscaled scale 18).
    pub price: i128,
    /// Trade quantity (unscaled scale 18).
    pub quantity: i128,
    /// Optional venue identifier.
    pub venue: Option<&'a str>,
    /// Optional trade identifier.
    pub trade_id: Option<&'a str>,
}

impl<'a> TradeEvent<'a> {
    /// Validate and return the trade.
    pub fn new(event: TradeEvent<'a>) -> Result<Self, Error> {
        event.validate()?;
        Ok(event)
    }

    /// Validate the trade against the Python record contract.
    pub fn validate(&self) -> Result<(), Error> {
        require_text(self.event_id)?;
        require_canonical_symbol(self.symbol)?;
        require_positive(self.price)?;
        require_positive(self.quantity)?;
        if let Some(venue) = self.venue {
            require_text(venue)?;
        }
        if let Some(trade_id) = self.trade_id {
            require_text(trade_id)?;
        }
        Ok(())
    }
}

/// The two market snapshots fill models and the simulated venue consume,
/// mirroring Python's `QuoteEvent | TradeEvent` union.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MarketEvent<'a> {
    /// A quote.
    Quote(QuoteEvent<'a>),
    /// A trade.
    Trade(TradeEvent<'a>),
}

impl<'a> MarketEvent<'a> {
    /// The snapshot's event identifier.
    pub fn event_id(&self) -> &'a str {
        match self {
            Self::Quote(event) => event.event_id,
            Self::Trade(event) => event.event_id,
        }
    }

    /// The snapshot's epoch-nanosecond timestamp.
    pub fn timestamp_ns(&self) -> i128 {
        match self {
            Self::Quote(event) => event.timestamp_ns,
            Self::Trade(event) => event.timestamp_ns,
        }
    }

    /// The snapshot's sequence number.
    pub fn sequence(&self) -> u64 {
        match self {
            Self::Quote(event) => event.sequence,
            Self::Trade(event) => event.sequence,
        }
    }

    /// The snapshot's canonical symbol.
    pub fn symbol(&self) -> &'a str {
        match self {
            Self::Quote(event) => event.symbol,
            Self::Trade(event) => event.symbol,
        }
    }

    /// The snapshot's event class.
    pub fn event_type(&self) -> EventType {
        match self {
            Self::Quote(_) => EventType::Quote,
            Self::Trade(_) => EventType::Trade,
        }
    }

    /// Class priority from the shared ordering contract.
    pub fn class_priority(&self) -> u8 {
        self.event_type().class_priority()
    }

    /// Validate the snapshot against the Python record contract.
    pub fn validate(&self) -> Result<(), Error> {
        match self {
            Self::Quote(event) => event.validate(),
            Self::Trade(event) => event.validate(),
        }
    }
}

fn prices_for_type(
    order_type: OrderType,
    limit_price: Option<i128>,
    stop_price: Option<i128>,
) -> Result<(), Error> {
    let (wants_limit, wants_stop) = match order_type {
        OrderType::Market => (false, false),
        OrderType::Limit => (true, false),
        OrderType::Stop => (false, true),
        OrderType::StopLimit => (true, true),
    };
    if limit_price.is_some() != wants_limit || stop_price.is_some() != wants_stop {
        return Err(Error::InvalidArgument);
    }
    if let Some(price) = limit_price {
        require_positive(price)?;
    }
    if let Some(price) = stop_price {
        require_positive(price)?;
    }
    Ok(())
}

/// A single-leg order with its execution prices (the view of Python's
/// `SingleLegOrder` consumed by fill models and the simulated venue).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MarketOrder<'a> {
    /// Order identifier.
    pub order_id: &'a str,
    /// Canonical symbol.
    pub symbol: &'a str,
    /// Order side.
    pub side: Side,
    /// Unscaled scale-18 quantity.
    pub quantity: i128,
    /// Order type.
    pub order_type: OrderType,
    /// Time in force.
    pub time_in_force: TimeInForce,
    /// Limit price when the type requires one.
    pub limit_price: Option<i128>,
    /// Stop price when the type requires one.
    pub stop_price: Option<i128>,
    /// Epoch nanoseconds.
    pub timestamp_ns: i128,
    /// Monotonic sequence number.
    pub sequence: u64,
}

impl<'a> MarketOrder<'a> {
    /// Build a market order with default envelope fields.
    pub fn market(order_id: &'a str, symbol: &'a str, side: Side, quantity: i128) -> Self {
        Self {
            order_id,
            symbol,
            side,
            quantity,
            order_type: OrderType::Market,
            time_in_force: TimeInForce::Day,
            limit_price: None,
            stop_price: None,
            timestamp_ns: 0,
            sequence: 0,
        }
    }

    /// Validate and return the order.
    pub fn new(order: MarketOrder<'a>) -> Result<Self, Error> {
        order.validate()?;
        Ok(order)
    }

    /// Validate the order against the Python record contract.
    pub fn validate(&self) -> Result<(), Error> {
        require_text(self.order_id)?;
        require_canonical_symbol(self.symbol)?;
        require_positive(self.quantity)?;
        prices_for_type(self.order_type, self.limit_price, self.stop_price)
    }
}
