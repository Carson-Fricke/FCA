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
            next_cost_basis = decimal::add(cost_basis, decimal::mul(signed, fill.price).map_err(decimal_error)?)
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
            next_cost_basis =
                decimal::mul(next_quantity, fill.price).map_err(decimal_error)?;
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
            decimal::sub(self.cash, decimal::mul(signed, fill.price).map_err(decimal_error)?)
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
        let next_cash_flow = decimal::add(self.ledgers[index].cash_flow, settlement_value)
            .map_err(decimal_error)?;
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
        self.markers
            .iter()
            .position(|entry| entry.symbol == symbol)
    }

    fn upsert_ledger(&mut self, ledger: Ledger) {
        match self.ledgers.binary_search_by(|existing| existing.symbol.as_str().cmp(ledger.symbol.as_str())) {
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
mod tests {
    use super::{Fill, VirtualBook};
    use crate::decimal::{from_int, mul_div, ONE};
    use crate::market::Side;

    fn book_fill<'a>(
        fill_id: &'a str,
        side: Side,
        quantity: i128,
        price: i128,
        fee: i128,
        symbol: &'a str,
    ) -> Fill<'a> {
        Fill {
            fill_id,
            order_id: "order-x",
            symbol,
            side,
            quantity,
            price,
            fee,
        }
    }

    #[test]
    fn average_cost_partial_close_reversal_and_fees_are_exact() {
        let mut book = VirtualBook::new("alpha", from_int(10_000));

        let first = book
            .apply_fill(&book_fill("buy-1", Side::Buy, from_int(10), from_int(100), ONE, "AAPL"))
            .unwrap();
        assert_eq!(first.quantity, from_int(10));
        assert_eq!(first.average_cost().unwrap(), from_int(100));
        assert_eq!(first.realized_pnl, -ONE);
        assert_eq!(first.unrealized_pnl().unwrap(), None);
        assert_eq!(book.cash(), from_int(8_999));
        assert_eq!(first.total_pnl().unwrap(), None);

        book.apply_fill(&book_fill(
            "buy-2",
            Side::Buy,
            from_int(5),
            from_int(110),
            500_000_000_000_000_000,
            "AAPL",
        ))
        .unwrap();
        let partial = book
            .apply_fill(&book_fill(
                "sell-part",
                Side::Sell,
                from_int(6),
                from_int(120),
                250_000_000_000_000_000,
                "AAPL",
            ))
            .unwrap();
        assert_eq!(partial.quantity, from_int(9));
        assert_eq!(partial.cost_basis, from_int(930));
        assert_eq!(partial.average_cost().unwrap(), 103_333_333_333_333_333_333);
        assert_eq!(partial.realized_pnl, 98_250_000_000_000_000_000);

        let reversed = book
            .apply_fill(&book_fill(
                "sell-through",
                Side::Sell,
                from_int(12),
                from_int(90),
                750_000_000_000_000_000,
                "AAPL",
            ))
            .unwrap();
        assert_eq!(reversed.quantity, -from_int(3));
        assert_eq!(reversed.cost_basis, -from_int(270));
        assert_eq!(reversed.average_cost().unwrap(), from_int(90));
        assert_eq!(reversed.realized_pnl, -22_500_000_000_000_000_000);
        assert_eq!(book.cash(), 10_247_500_000_000_000_000_000);

        let marker = book.mark("AAPL", from_int(85)).unwrap();
        let marked = book.position("AAPL").unwrap();
        assert_eq!(marker.price, from_int(85));
        assert_eq!(marked.mark_price, Some(from_int(85)));
        assert_eq!(marked.unrealized_pnl().unwrap(), Some(from_int(15)));
        assert_eq!(marked.total_pnl().unwrap(), Some(-7_500_000_000_000_000_000));

        let snap = book.snapshot();
        assert_eq!(snap.equity().unwrap(), Some(9_992_500_000_000_000_000_000));
        assert_eq!(snap.total_pnl().unwrap(), Some(-7_500_000_000_000_000_000));
        let unrealized = snap.unrealized_pnl().unwrap().unwrap();
        assert_eq!(unrealized, from_int(15));
        assert_eq!(
            snap.total_pnl().unwrap(),
            Some(crate::decimal::add(snap.realized_pnl().unwrap(), unrealized).unwrap())
        );
    }

    #[test]
    fn flat_position_retains_realized_pnl_and_needs_no_marker() {
        let mut book = VirtualBook::new("round-trip", from_int(1_000));
        book.apply_fill(&book_fill("buy", Side::Buy, from_int(2), from_int(10), 0, "AAPL"))
            .unwrap();
        book.apply_fill(&book_fill(
            "buy-recurring-average",
            Side::Buy,
            ONE,
            from_int(11),
            0,
            "AAPL",
        ))
        .unwrap();
        book.apply_fill(&book_fill(
            "partial-close",
            Side::Sell,
            ONE,
            from_int(12),
            0,
            "AAPL",
        ))
        .unwrap();
        let closed = book
            .apply_fill(&book_fill(
                "sell",
                Side::Sell,
                from_int(2),
                from_int(10),
                100_000_000_000_000_000,
                "AAPL",
            ))
            .unwrap();

        assert!(closed.is_flat());
        assert_eq!(closed.average_cost().unwrap(), 0);
        assert_eq!(closed.cost_basis, 0);
        assert_eq!(closed.realized_pnl, 900_000_000_000_000_000);
        assert_eq!(closed.market_value().unwrap(), Some(0));
        assert_eq!(closed.unrealized_pnl().unwrap(), Some(0));
        assert_eq!(book.cash(), 1_000_900_000_000_000_000_000);

        let snap = book.snapshot();
        assert_eq!(snap.equity().unwrap(), Some(1_000_900_000_000_000_000_000));
        assert_eq!(snap.total_pnl().unwrap(), Some(900_000_000_000_000_000));
    }

    #[test]
    fn duplicate_fill_and_invalid_marker_leave_book_unchanged() {
        let mut book = VirtualBook::new("alpha", from_int(100));
        let applied = book_fill("one", Side::Buy, ONE, from_int(10), 0, "AAPL");
        book.apply_fill(&applied).unwrap();

        assert_eq!(book.apply_fill(&applied), Err(super::Error::DuplicateFill));
        assert_eq!(book.mark("AAPL", 0), Err(super::Error::InvalidArgument));

        assert_eq!(book.cash(), from_int(90));
        assert_eq!(book.fill_count(), 1);
        assert_eq!(book.marker_count(), 0);
    }

    #[test]
    fn retire_position_finalizes_expired_contracts_exactly() {
        let mut book = VirtualBook::new("expiry", from_int(1_000));
        book.apply_fill(&book_fill(
            "buy-option",
            Side::Buy,
            ONE,
            from_int(200),
            0,
            "AAPL240119P00100000",
        ))
        .unwrap();
        let worthless = book.retire_position("AAPL240119P00100000", 0).unwrap();

        assert!(worthless.is_flat());
        assert_eq!(worthless.cost_basis, 0);
        assert_eq!(worthless.realized_pnl, -200_000_000_000_000_000_000);
        assert_eq!(worthless.market_value().unwrap(), Some(0));
        assert_eq!(book.cash(), from_int(800));

        let snap = book.snapshot();
        assert_eq!(snap.equity().unwrap(), Some(from_int(800)));
        assert_eq!(snap.total_pnl().unwrap(), Some(-200_000_000_000_000_000_000));

        let mut assigned = VirtualBook::new("assignment", from_int(1_000));
        assigned
            .apply_fill(&book_fill(
                "sell-option",
                Side::Sell,
                ONE,
                from_int(100),
                0,
                "AAPL240119P00100000",
            ))
            .unwrap();
        let settled = assigned
            .retire_position("AAPL240119P00100000", -from_int(250))
            .unwrap();
        assert_eq!(settled.realized_pnl, -150_000_000_000_000_000_000);
        assert_eq!(assigned.cash(), from_int(850));

        assert_eq!(
            book.retire_position("AAPL240119P00100000", 0),
            Err(super::Error::PositionFlat)
        );
        assert_eq!(
            book.retire_position("AAPL", 0),
            Err(super::Error::UnknownPosition)
        );
    }

    #[test]
    fn snapshot_positions_are_ordered_by_canonical_symbol() {
        let mut book = VirtualBook::new("ordered", from_int(1_000));
        book.apply_fill(&book_fill("msft", Side::Buy, ONE, from_int(10), 0, "MSFT"))
            .unwrap();
        book.apply_fill(&book_fill("aapl", Side::Buy, ONE, from_int(10), 0, "AAPL"))
            .unwrap();
        let snap = book.snapshot();
        assert_eq!(snap.positions.len(), 2);
        assert_eq!(snap.positions[0].symbol, "AAPL");
        assert_eq!(snap.positions[1].symbol, "MSFT");
        assert_eq!(snap.fill_count, 2);
    }

    #[test]
    fn ten_thousand_generated_fill_sequences_conserve_cash_and_position() {
        let initial_cash = from_int(100_000);
        for case_index in 0..10_000usize {
            let mut book = VirtualBook::new("generated", initial_cash);
            let mut expected_cash: i128 = initial_cash;
            let mut expected_quantity: i128 = 0;

            for step in 0..5usize {
                let buy = ((case_index >> step) + step) % 2 == 0;
                let side = if buy { Side::Buy } else { Side::Sell };
                let quantity: i128 = ((case_index * (step + 3) + step) % 7 + 1) as i128 * ONE;
                let price: i128 = (9000 + (case_index * 37 + step * 101) % 3001) as i128 * ONE / 100;
                let fee: i128 = (((case_index + step * 3) % 7) as i128 - 2) * ONE / 100;
                let signed: i128 = if buy { quantity } else { -quantity };
                expected_cash -= mul_div(signed, price, ONE).unwrap() + fee;
                expected_quantity += signed;

                let fill_id = format!("{case_index}-{step}");
                book.apply_fill(&Fill {
                    fill_id: &fill_id,
                    order_id: "order-generated",
                    symbol: "AAPL",
                    side,
                    quantity,
                    price,
                    fee,
                })
                .unwrap();
            }

            let mark: i128 = (9500 + case_index % 2001) as i128 * ONE / 100;
            book.mark("AAPL", mark).unwrap();
            let position = book.position("AAPL").unwrap();
            let snap = book.snapshot();

            assert_eq!(expected_cash, book.cash());
            assert_eq!(expected_quantity, position.quantity);
            let expected_market_value = mul_div(expected_quantity, mark, ONE).unwrap();
            assert_eq!(snap.market_value().unwrap(), Some(expected_market_value));
            assert_eq!(
                snap.equity().unwrap(),
                Some(book.cash() + expected_market_value)
            );
            assert_eq!(
                snap.total_pnl().unwrap(),
                Some(
                    crate::decimal::add(
                        snap.realized_pnl().unwrap(),
                        snap.unrealized_pnl().unwrap().unwrap()
                    )
                    .unwrap()
                )
            );
            assert_eq!(snap.fill_count, 5);
        }
    }
}
