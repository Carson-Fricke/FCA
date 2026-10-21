//! Author-facing event and start views.
//!
//! These mirror the engine's `EngineMarket`/`EngineFill` shapes exactly, with
//! borrowed string fields: the underlying event data is owned by the engine
//! and valid for the duration of one callback, so authors read without any
//! per-event allocation. Prices are exact [`Decimal`] values.

use num::Decimal;
use std::collections::BTreeMap;

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
    pub bid: Decimal,
    /// Best ask.
    pub ask: Decimal,
    /// Bid size.
    pub bid_quantity: Decimal,
    /// Ask size.
    pub ask_quantity: Decimal,
    /// Reporting venue.
    pub venue: &'a str,
}

impl<'a> QuoteEvent<'a> {
    /// The quote midpoint, or the bid when the midpoint cannot be formed.
    #[must_use]
    pub fn mid(&self) -> Decimal {
        self.bid
            .checked_add(self.ask)
            .and_then(|sum| sum.checked_div(Decimal::from_i128(2)))
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
    pub price: Decimal,
    /// Print quantity.
    pub quantity: Decimal,
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
    pub fn notional(&self) -> Result<Decimal, num::DecimalError> {
        self.price.checked_mul(self.quantity)
    }
}

/// One OHLCV bar as the engine dispatched it.
///
/// Consumers read these bars inside an atomic [`BarFrame`]. Every decimal field
/// is exact, without a float roundtrip.
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
    pub open: Decimal,
    /// Interval high.
    pub high: Decimal,
    /// Interval low.
    pub low: Decimal,
    /// Interval close.
    pub close: Decimal,
    /// Interval volume.
    pub volume: Decimal,
}

/// One complete atomic interval delivered to a study, including missing symbols.
/// The map is constructed locally by the SDK and never crosses the C ABI.
#[derive(Clone, Debug)]
pub struct BarFrame<'a> {
    /// Canonical interval identity: positive minute/hour cadence or one session day.
    pub interval: &'a str,
    /// Inclusive interval start in epoch nanoseconds.
    pub interval_start_ns: i64,
    /// Exclusive interval end in epoch nanoseconds; strictly after start.
    pub interval_end_ns: i64,
    /// Availability time, at or after interval end.
    pub available_at_ns: i64,
    /// Exact bars by symbol; every timestamp equals interval start.
    pub bars_by_symbol: BTreeMap<&'a str, BarEvent<'a>>,
    /// Unique absent symbols, disjoint from the bars.
    pub missing_symbols: Vec<&'a str>,
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
    /// This fill's quantity, not the order's requested or cumulative quantity.
    pub quantity: Decimal,
    /// Fill price.
    pub price: Decimal,
    /// Fill fee.
    pub fee: Decimal,
    /// Liquidity role.
    pub liquidity: &'a str,
}

impl<'a> Fill<'a> {
    /// The signed quantity against the current position.
    #[must_use]
    pub fn signed_quantity(&self) -> Decimal {
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
    pub initial_cash: Decimal,
    /// Run start in epoch nanoseconds.
    pub start_ns: i64,
}
