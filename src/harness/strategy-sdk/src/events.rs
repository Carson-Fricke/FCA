//! Author-facing event and start views.
//!
//! These mirror the engine's `EngineMarket`/`EngineFill` shapes exactly, with
//! borrowed string fields: the underlying event data is owned by the engine
//! and valid for the duration of one callback, so authors read without any
//! per-event allocation. Prices are exact [`Decimal`] values.

use num::PyDecimal;

/// A top-of-book quote as the engine dispatched it.
#[derive(Clone, Copy, Debug)]
pub struct QuoteEvent<'a> {
    /// Event identifier.
    pub event_id: &'a str,
    /// Event time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Global sequence.
    pub sequence: u64,
    /// Instrument symbol.
    pub symbol: &'a str,
    /// Best bid.
    pub bid: PyDecimal,
    /// Best ask.
    pub ask: PyDecimal,
    /// Bid size.
    pub bid_quantity: PyDecimal,
    /// Ask size.
    pub ask_quantity: PyDecimal,
    /// Reporting venue.
    pub venue: &'a str,
}

impl<'a> QuoteEvent<'a> {
    /// The quote midpoint, or the bid when the midpoint cannot be formed.
    #[must_use]
    pub fn mid(&self) -> PyDecimal {
        self.bid
            .checked_add(self.ask)
            .and_then(|sum| sum.checked_div(PyDecimal::from_i128(2)))
            .unwrap_or(self.bid)
    }
}

/// A trade print as the engine dispatched it.
#[derive(Clone, Copy, Debug)]
pub struct TradeEvent<'a> {
    /// Event identifier.
    pub event_id: &'a str,
    /// Event time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Global sequence.
    pub sequence: u64,
    /// Instrument symbol.
    pub symbol: &'a str,
    /// Print price.
    pub price: PyDecimal,
    /// Print quantity.
    pub quantity: PyDecimal,
    /// Reporting venue.
    pub venue: &'a str,
    /// Trade identifier.
    pub trade_id: &'a str,
}

impl<'a> TradeEvent<'a> {
    /// The notional value of the print (price times quantity).
    ///
    /// # Errors
    ///
    /// Returns an error when the exact product does not fit the pinned
    /// arithmetic context.
    pub fn notional(&self) -> Result<PyDecimal, num::DecimalError> {
        self.price.checked_mul(self.quantity)
    }
}

/// One OHLCV bar as the engine dispatched it.
///
/// This is the only market event a study program folds; quotes and trades
/// remain on [`StrategyProgram`](crate::program::StrategyProgram) and the
/// indicator surface. Every decimal field is exact, so a study reads bar
/// values without a float roundtrip.
#[derive(Clone, Copy, Debug)]
pub struct BarEvent<'a> {
    /// Event identifier.
    pub event_id: &'a str,
    /// Event time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Global sequence.
    pub sequence: u64,
    /// Instrument symbol.
    pub symbol: &'a str,
    /// Interval open.
    pub open: PyDecimal,
    /// Interval high.
    pub high: PyDecimal,
    /// Interval low.
    pub low: PyDecimal,
    /// Interval close.
    pub close: PyDecimal,
    /// Interval volume.
    pub volume: PyDecimal,
}

/// One booked fill owned by the program.
#[derive(Clone, Copy, Debug)]
pub struct Fill<'a> {
    /// Fill identifier (also the report event identifier).
    pub event_id: &'a str,
    /// Fill time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Fill sequence.
    pub sequence: u64,
    /// Fill identifier.
    pub fill_id: &'a str,
    /// Order identifier.
    pub order_id: &'a str,
    /// Instrument symbol.
    pub symbol: &'a str,
    /// `buy` or `sell`.
    pub side: &'a str,
    /// Fill quantity.
    pub quantity: PyDecimal,
    /// Fill price.
    pub price: PyDecimal,
    /// Fill fee.
    pub fee: PyDecimal,
    /// Liquidity role.
    pub liquidity: &'a str,
}

impl<'a> Fill<'a> {
    /// The signed quantity against the current position.
    #[must_use]
    pub fn signed_quantity(&self) -> PyDecimal {
        if self.side == "buy" {
            self.quantity
        } else {
            self.quantity.negated()
        }
    }
}

/// Startup facts handed to a strategy program.
#[derive(Clone, Copy, Debug)]
pub struct ProgramStart<'a> {
    /// Instance identifier.
    pub instance_id: &'a str,
    /// Shared account identifier.
    pub account_id: &'a str,
    /// Strategy universe symbols.
    pub symbols: &'a [&'a str],
    /// Run seed.
    pub seed: u64,
    /// Initial book cash.
    pub initial_cash: PyDecimal,
    /// Run start in epoch nanoseconds.
    pub start_ns: i64,
}
