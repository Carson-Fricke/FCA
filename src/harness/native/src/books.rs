//! Decimal-exact accounting for one strategy's virtual book, ported from
//! `src/harness/portfolio/book.py` onto the pinned fixed-point kernel.
//!
//! Cash, positions, average cost, and realized/unrealized PnL use the scale-18
//! `i128` fixed-point kernel (`decimal128(38, 18)`). Fill prices determine
//! average cost; fees, including negative rebate fees, are recognized
//! immediately in realized PnL; marks are explicit and never mutate cost or
//! realized PnL. Position ledgers are kept sorted by canonical symbol bytes,
//! matching Python's `sorted(..., key=str)` snapshot order.

use crate::decimal::{self, DecimalError};
use crate::market::Side;

/// A checked failure from the virtual book.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// Allocation failure.
    OutOfMemory,
    /// A required argument was invalid.
    InvalidArgument,
    /// Fixed-point overflow or a non-representable value.
    OutOfRange,
    /// Division by zero or otherwise invalid decimal operation.
    InvalidDecimal,
    /// A fill identifier was accounted more than once.
    DuplicateFill,
    /// The symbol was never held by this book.
    UnknownPosition,
    /// The ledger is already flat.
    PositionFlat,
}

impl core::fmt::Display for Error {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::OutOfMemory => "out of memory",
            Self::InvalidArgument => "invalid argument",
            Self::OutOfRange => "fixed-point result is outside decimal128(38, 18)",
            Self::InvalidDecimal => "invalid decimal operation",
            Self::DuplicateFill => "duplicate fill",
            Self::UnknownPosition => "unknown position",
            Self::PositionFlat => "position is already flat",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for Error {}

fn decimal_error(error: DecimalError) -> Error {
    match error {
        DecimalError::OutOfRange => Error::OutOfRange,
        DecimalError::InvalidDecimal => Error::InvalidDecimal,
    }
}

/// One immutable execution fill. Quantities are unscaled at scale 18;
/// `quantity`/`price` are positive and `fee` may have any sign (negative fees
/// represent liquidity rebates).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Fill<'a> {
    /// Fill identifier.
    pub fill_id: &'a str,
    /// Originating order identifier.
    pub order_id: &'a str,
    /// Canonical symbol.
    pub symbol: &'a str,
    /// Fill side.
    pub side: Side,
    /// Unscaled scale-18 quantity.
    pub quantity: i128,
    /// Unscaled scale-18 price.
    pub price: i128,
    /// Unscaled scale-18 fee (negative for rebates).
    pub fee: i128,
}

/// An explicit mark used to value a position without consulting a clock.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PositionMarker {
    /// Canonical symbol.
    pub symbol: String,
    /// Unscaled scale-18 mark price.
    pub price: i128,
}

/// Immutable signed position, cost basis, and attributed PnL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PositionSnapshot {
    /// Canonical symbol.
    pub symbol: String,
    /// Signed quantity.
    pub quantity: i128,
    /// Cost basis.
    pub cost_basis: i128,
    /// Cash flow attributed to this position.
    pub cash_flow: i128,
    /// Realized PnL including fees.
    pub realized_pnl: i128,
    /// Optional explicit mark price.
    pub mark_price: Option<i128>,
}

impl PositionSnapshot {
    /// True when the position is flat.
    pub fn is_flat(&self) -> bool {
        self.quantity == 0
    }

    /// Average entry price, or zero for a flat position.
    pub fn average_cost(&self) -> Result<i128, Error> {
        if self.is_flat() {
            return Ok(0);
        }
        decimal::div(self.cost_basis, self.quantity).map_err(decimal_error)
    }

    /// Signed marked value; `None` only when open and unmarked.
    pub fn market_value(&self) -> Result<Option<i128>, Error> {
        if self.is_flat() {
            return Ok(Some(0));
        }
        match self.mark_price {
            None => Ok(None),
            Some(price) => decimal::mul(self.quantity, price)
                .map(Some)
                .map_err(decimal_error),
        }
    }

    /// Exact marked change from this position's cash flows.
    pub fn total_pnl(&self) -> Result<Option<i128>, Error> {
        let Some(market_value) = self.market_value()? else {
            return Ok(None);
        };
        decimal::add(self.cash_flow, market_value)
            .map(Some)
            .map_err(decimal_error)
    }

    /// Marked PnL as the exact remainder after realized PnL.
    pub fn unrealized_pnl(&self) -> Result<Option<i128>, Error> {
        let Some(total) = self.total_pnl()? else {
            return Ok(None);
        };
        decimal::sub(total, self.realized_pnl)
            .map(Some)
            .map_err(decimal_error)
    }
}

/// Immutable valuation of one strategy virtual book.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BookSnapshot {
    /// Book identifier.
    pub book_id: String,
    /// Initial cash.
    pub initial_cash: i128,
    /// Current cash.
    pub cash: i128,
    /// Position snapshots in canonical symbol order.
    pub positions: Vec<PositionSnapshot>,
    /// Number of fills accounted.
    pub fill_count: usize,
}

impl BookSnapshot {
    /// Realized PnL across all symbols, including all fill fees.
    pub fn realized_pnl(&self) -> Result<i128, Error> {
        let mut total: i128 = 0;
        for snapshot in &self.positions {
            total = decimal::add(total, snapshot.realized_pnl).map_err(decimal_error)?;
        }
        Ok(total)
    }

    /// Total unrealized PnL, or `None` if an open leg is unmarked.
    pub fn unrealized_pnl(&self) -> Result<Option<i128>, Error> {
        let Some(total) = self.total_pnl()? else {
            return Ok(None);
        };
        decimal::sub(total, self.realized_pnl()?)
            .map(Some)
            .map_err(decimal_error)
    }

    /// Total signed market value when every open leg is marked.
    pub fn market_value(&self) -> Result<Option<i128>, Error> {
        let mut total: i128 = 0;
        for snapshot in &self.positions {
            let Some(value) = snapshot.market_value()? else {
                return Ok(None);
            };
            total = decimal::add(total, value).map_err(decimal_error)?;
        }
        Ok(Some(total))
    }

    /// Marked book equity, or `None` while a leg is unmarked.
    pub fn equity(&self) -> Result<Option<i128>, Error> {
        let Some(market_value) = self.market_value()? else {
            return Ok(None);
        };
        decimal::add(self.cash, market_value)
            .map(Some)
            .map_err(decimal_error)
    }

    /// The exact marked change in book equity.
    pub fn total_pnl(&self) -> Result<Option<i128>, Error> {
        let Some(marked_equity) = self.equity()? else {
            return Ok(None);
        };
        decimal::sub(marked_equity, self.initial_cash)
            .map(Some)
            .map_err(decimal_error)
    }

    /// Return the snapshot for one previously held symbol.
    pub fn position(&self, symbol: &str) -> Result<PositionSnapshot, Error> {
        self.positions
            .iter()
            .find(|snapshot| snapshot.symbol == symbol)
            .cloned()
            .ok_or(Error::UnknownPosition)
    }
}

#[derive(Clone, Debug)]
struct Ledger {
    symbol: String,
    quantity: i128,
    cost_basis: i128,
    cash_flow: i128,
    realized_pnl: i128,
}

#[derive(Clone, Debug)]
struct MarkerEntry {
    symbol: String,
    price: i128,
}

/// Maintain cash and average-cost positions for one strategy, mirroring
/// Python's `VirtualBook` ledger algebra on the pinned fixed-point kernel.
#[derive(Clone, Debug, Default)]
pub struct VirtualBook {
    book_id: String,
    initial_cash: i128,
    cash: i128,
    ledgers: Vec<Ledger>,
    markers: Vec<MarkerEntry>,
    fill_ids: Vec<String>,
}

impl VirtualBook {
    /// Create an empty book.
    pub fn new(book_id: &str, initial_cash: i128) -> Self {
        Self {
            book_id: book_id.to_owned(),
            initial_cash,
            cash: initial_cash,
            ledgers: Vec::new(),
            markers: Vec::new(),
            fill_ids: Vec::new(),
        }
    }

    /// The book identifier.
    pub fn book_id(&self) -> &str {
        &self.book_id
    }

    /// Current cash.
    pub fn cash(&self) -> i128 {
        self.cash
    }

    /// Number of accounted fills.
    pub fn fill_count(&self) -> usize {
        self.fill_ids.len()
    }

    /// Number of explicit markers.
    pub fn marker_count(&self) -> usize {
        self.markers.len()
    }

    /// Atomically account one immutable core fill. The ledger algebra is the
    /// exact port of `VirtualBook.apply_fill`.
    pub fn apply_fill(&mut self, fill: &Fill<'_>) -> Result<PositionSnapshot, Error> {
        if fill.fill_id.is_empty() || fill.order_id.is_empty() || fill.symbol.is_empty() {
            return Err(Error::InvalidArgument);
        }
        if fill.quantity <= 0 || fill.price <= 0 {
            return Err(Error::InvalidArgument);
        }
        if self.fill_ids.iter().any(|seen| seen == fill.fill_id) {
            return Err(Error::DuplicateFill);
        }

        let signed: i128 = if fill.side == Side::Buy {
            fill.quantity
        } else {
            -fill.quantity
        };
        let index = self.ledger_index(fill.symbol);
        let (quantity, cost_basis, cash_flow, mut realized_pnl) = match index {
            Some(index) => {
                let ledger = &self.ledgers[index];
                (
                    ledger.quantity,
                    ledger.cost_basis,
                    ledger.cash_flow,
                    ledger.realized_pnl,
                )
            }
            None => (0, 0, 0, 0),
        };
        let next_quantity = decimal::add(quantity, signed).map_err(decimal_error)?;

        let mut next_cost_basis: i128;
        if quantity == 0 || (quantity > 0) == (signed > 0) {
            next_cost_basis = decimal::add(
                cost_basis,
                decimal::mul(signed, fill.price).map_err(decimal_error)?,
            )
            .map_err(decimal_error)?;
        } else if abs_less_or_equal(signed, quantity) {
            let released_basis = if abs_equal(signed, quantity) {
                cost_basis
            } else {
                decimal::mul_div(cost_basis, abs_value(signed), abs_value(quantity))
                    .map_err(decimal_error)?
            };
            realized_pnl = decimal::add(
                realized_pnl,
                decimal::sub(
                    decimal::mul(-signed, fill.price).map_err(decimal_error)?,
                    released_basis,
                )
                .map_err(decimal_error)?,
            )
            .map_err(decimal_error)?;
            next_cost_basis = decimal::sub(cost_basis, released_basis).map_err(decimal_error)?;
        } else {
            let closing_quantity = -quantity;
            realized_pnl = decimal::add(
                realized_pnl,
                decimal::sub(
                    decimal::mul(-closing_quantity, fill.price).map_err(decimal_error)?,
                    cost_basis,
                )
                .map_err(decimal_error)?,
            )
            .map_err(decimal_error)?;
            next_cost_basis = decimal::mul(next_quantity, fill.price).map_err(decimal_error)?;
        }

        realized_pnl = decimal::sub(realized_pnl, fill.fee).map_err(decimal_error)?;
        let next_cash_flow = decimal::sub(
            decimal::sub(
                cash_flow,
                decimal::mul(signed, fill.price).map_err(decimal_error)?,
            )
            .map_err(decimal_error)?,
            fill.fee,
        )
        .map_err(decimal_error)?;
        if next_quantity == 0 {
            next_cost_basis = 0;
            realized_pnl = next_cash_flow;
        }
        let next_cash = decimal::sub(
            decimal::sub(
                self.cash,
                decimal::mul(signed, fill.price).map_err(decimal_error)?,
            )
            .map_err(decimal_error)?,
            fill.fee,
        )
        .map_err(decimal_error)?;

        let stored_symbol = match index {
            Some(index) => self.ledgers[index].symbol.clone(),
            None => fill.symbol.to_owned(),
        };
        self.upsert_ledger(Ledger {
            symbol: stored_symbol,
            quantity: next_quantity,
            cost_basis: next_cost_basis,
            cash_flow: next_cash_flow,
            realized_pnl,
        });
        self.cash = next_cash;
        self.fill_ids.push(fill.fill_id.to_owned());
        self.position(fill.symbol)
    }

    /// Retire one expired contract ledger at its final settlement value.
    pub fn retire_position(
        &mut self,
        symbol: &str,
        settlement_value: i128,
    ) -> Result<PositionSnapshot, Error> {
        let index = self.ledger_index(symbol).ok_or(Error::UnknownPosition)?;
        if self.ledgers[index].quantity == 0 {
            return Err(Error::PositionFlat);
        }
        let next_cash_flow =
            decimal::add(self.ledgers[index].cash_flow, settlement_value).map_err(decimal_error)?;
        let stored_symbol = self.ledgers[index].symbol.clone();
        self.ledgers[index] = Ledger {
            symbol: stored_symbol,
            quantity: 0,
            cost_basis: 0,
            cash_flow: next_cash_flow,
            realized_pnl: next_cash_flow,
        };
        self.cash = decimal::add(self.cash, settlement_value).map_err(decimal_error)?;
        self.position(self.ledgers[index].symbol.as_str())
    }

    /// Create and apply an explicit price marker.
    pub fn mark(&mut self, symbol: &str, price: i128) -> Result<PositionMarker, Error> {
        if symbol.is_empty() || price <= 0 {
            return Err(Error::InvalidArgument);
        }
        if let Some(index) = self.marker_index(symbol) {
            self.markers[index].price = price;
            return Ok(PositionMarker {
                symbol: self.markers[index].symbol.clone(),
                price,
            });
        }
        self.markers.push(MarkerEntry {
            symbol: symbol.to_owned(),
            price,
        });
        Ok(PositionMarker {
            symbol: symbol.to_owned(),
            price,
        })
    }

    /// Return the current explicit marker for a symbol, if any.
    pub fn marker(&self, symbol: &str) -> Option<PositionMarker> {
        let index = self.marker_index(symbol)?;
        Some(PositionMarker {
            symbol: self.markers[index].symbol.clone(),
            price: self.markers[index].price,
        })
    }

    /// Return the current immutable state for one previously held symbol.
    pub fn position(&self, symbol: &str) -> Result<PositionSnapshot, Error> {
        let index = self.ledger_index(symbol).ok_or(Error::UnknownPosition)?;
        Ok(self.position_snapshot(index))
    }

    /// Return the current immutable book valuation. Positions are ordered by
    /// canonical symbol.
    pub fn snapshot(&self) -> BookSnapshot {
        BookSnapshot {
            book_id: self.book_id.clone(),
            initial_cash: self.initial_cash,
            cash: self.cash,
            positions: (0..self.ledgers.len())
                .map(|index| self.position_snapshot(index))
                .collect(),
            fill_count: self.fill_ids.len(),
        }
    }

    fn position_snapshot(&self, index: usize) -> PositionSnapshot {
        let ledger = &self.ledgers[index];
        PositionSnapshot {
            symbol: ledger.symbol.clone(),
            quantity: ledger.quantity,
            cost_basis: ledger.cost_basis,
            cash_flow: ledger.cash_flow,
            realized_pnl: ledger.realized_pnl,
            mark_price: self
                .marker_index(&ledger.symbol)
                .map(|marker| self.markers[marker].price),
        }
    }

    fn ledger_index(&self, symbol: &str) -> Option<usize> {
        self.ledgers
            .iter()
            .position(|ledger| ledger.symbol == symbol)
    }

    fn marker_index(&self, symbol: &str) -> Option<usize> {
        self.markers.iter().position(|entry| entry.symbol == symbol)
    }

    fn upsert_ledger(&mut self, ledger: Ledger) {
        match self
            .ledgers
            .binary_search_by(|existing| existing.symbol.as_str().cmp(ledger.symbol.as_str()))
        {
            Ok(index) => self.ledgers[index] = ledger,
            Err(index) => self.ledgers.insert(index, ledger),
        }
    }
}

fn abs_value(value: i128) -> i128 {
    if value < 0 {
        -value
    } else {
        value
    }
}

fn abs_equal(a: i128, b: i128) -> bool {
    abs_value(a) == abs_value(b)
}

fn abs_less_or_equal(a: i128, b: i128) -> bool {
    abs_value(a) <= abs_value(b)
}

#[cfg(test)]
#[path = "../tests/unit/books.rs"]
mod tests;
