//! Multiple virtual books and their shared-account aggregate.

use std::collections::HashMap;

use harness_num::Decimal18;

use crate::accounting::Fill;
use crate::error::{require_text, LiveError, LiveResult};
use crate::portfolio::book::{BookSnapshot, PositionMarker, PositionSnapshot, VirtualBook};
use crate::symbol::{canonical_symbol, Symbol};

const ZERO: Decimal18 = Decimal18::ZERO;

/// Shared-account totals retaining virtual-book PnL attribution.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AggregatePosition {
    /// The aggregated symbol.
    pub symbol: Symbol,
    /// Net signed quantity.
    pub quantity: Decimal18,
    /// Sum of long quantities.
    pub long_quantity: Decimal18,
    /// Sum of absolute short quantities.
    pub short_quantity: Decimal18,
    /// Total cost basis.
    pub cost_basis: Decimal18,
    /// Total cash flow.
    pub cash_flow: Decimal18,
    /// Average cost, or `None` across a mixed-sign aggregate.
    pub average_cost: Option<Decimal18>,
    /// Total realized PnL.
    pub realized_pnl: Decimal18,
    /// The common mark price, if every open leg shares one.
    pub mark_price: Option<Decimal18>,
    /// Total signed market value, or `None` while a leg is unmarked.
    pub market_value: Option<Decimal18>,
    /// Total unrealized PnL, or `None` while a leg is unmarked.
    pub unrealized_pnl: Option<Decimal18>,
}

impl AggregatePosition {
    /// Long quantity plus the absolute short quantity.
    pub fn gross_quantity(&self) -> Decimal18 {
        self.long_quantity + self.short_quantity
    }

    /// The exact marked change from constituent cash flows.
    pub fn total_pnl(&self) -> Option<Decimal18> {
        self.market_value.map(|value| self.cash_flow + value)
    }
}

/// Immutable shared-account aggregate and constituent book snapshots.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PortfolioSnapshot {
    /// Account identifier.
    pub account_id: String,
    /// Sum of constituent opening cash.
    pub initial_cash: Decimal18,
    /// Sum of constituent cash.
    pub cash: Decimal18,
    /// Aggregated positions.
    pub positions: Vec<AggregatePosition>,
    /// Constituent book snapshots.
    pub books: Vec<BookSnapshot>,
    /// Total accounted fills.
    pub fill_count: usize,
}

impl PortfolioSnapshot {
    /// Realized PnL attributed across all virtual books.
    pub fn realized_pnl(&self) -> Decimal18 {
        self.positions
            .iter()
            .fold(ZERO, |sum, position| sum + position.realized_pnl)
    }

    /// Attributed unrealized PnL when every open leg is marked.
    pub fn unrealized_pnl(&self) -> Option<Decimal18> {
        self.total_pnl().map(|total| total - self.realized_pnl())
    }

    /// Total signed market value when every open leg is marked.
    pub fn market_value(&self) -> Option<Decimal18> {
        let mut total = ZERO;
        for position in &self.positions {
            total += position.market_value?;
        }
        Some(total)
    }

    /// Shared marked equity.
    pub fn equity(&self) -> Option<Decimal18> {
        self.market_value().map(|value| self.cash + value)
    }

    /// The exact change in aggregate equity.
    pub fn total_pnl(&self) -> Option<Decimal18> {
        self.equity().map(|equity| equity - self.initial_cash)
    }

    /// Return aggregate state for one symbol.
    pub fn position(&self, symbol: &str) -> LiveResult<AggregatePosition> {
        let canonical = canonical_symbol(symbol)?;
        self.positions
            .iter()
            .find(|position| position.symbol == canonical)
            .cloned()
            .ok_or_else(|| LiveError::Portfolio(format!("unknown position {symbol:?}")))
    }

    /// Return one constituent immutable book snapshot.
    pub fn book(&self, book_id: &str) -> LiveResult<BookSnapshot> {
        self.books
            .iter()
            .find(|book| book.book_id == book_id)
            .cloned()
            .ok_or_else(|| LiveError::Portfolio(format!("unknown book {book_id:?}")))
    }
}

/// Route fills to independent books and expose shared-account totals.
#[derive(Clone, Debug)]
pub struct PortfolioHub {
    account_id: String,
    books: Vec<VirtualBook>,
    book_index: HashMap<String, usize>,
    fill_routes: HashMap<String, String>,
    markers: HashMap<Symbol, PositionMarker>,
}

impl PortfolioHub {
    /// Construct a hub, registering any supplied books.
    pub fn new(account_id: &str, books: Vec<VirtualBook>) -> LiveResult<Self> {
        let mut hub = Self {
            account_id: require_text(account_id, "account_id")?,
            books: Vec::new(),
            book_index: HashMap::new(),
            fill_routes: HashMap::new(),
            markers: HashMap::new(),
        };
        for book in books {
            hub.register_book(book)?;
        }
        Ok(hub)
    }

    /// The account identifier.
    pub fn account_id(&self) -> &str {
        &self.account_id
    }

    /// Registered books in registration order.
    pub fn books(&self) -> &[VirtualBook] {
        &self.books
    }

    /// Registered book identifiers in registration order.
    pub fn book_ids(&self) -> Vec<&str> {
        self.books.iter().map(|book| book.book_id()).collect()
    }

    /// The current constituent and shared-account valuations.
    pub fn snapshot(&self) -> PortfolioSnapshot {
        let books: Vec<BookSnapshot> = self.books.iter().map(VirtualBook::snapshot).collect();
        let mut positions_by_symbol: HashMap<Symbol, Vec<PositionSnapshot>> = HashMap::new();
        for book in &books {
            for position in &book.positions {
                positions_by_symbol
                    .entry(position.symbol.clone())
                    .or_default()
                    .push(position.clone());
            }
        }
        let mut symbols: Vec<&Symbol> = positions_by_symbol.keys().collect();
        symbols.sort();
        let positions = symbols
            .into_iter()
            .map(|symbol| aggregate_position(symbol, &positions_by_symbol[symbol]))
            .collect();
        PortfolioSnapshot {
            account_id: self.account_id.clone(),
            initial_cash: books.iter().fold(ZERO, |sum, book| sum + book.initial_cash),
            cash: books.iter().fold(ZERO, |sum, book| sum + book.cash),
            positions,
            fill_count: books.iter().map(|book| book.fill_count).sum(),
            books,
        }
    }

    /// Aggregate strategy cash.
    pub fn cash(&self) -> Decimal18 {
        self.snapshot().cash
    }

    /// Aggregate attributed realized PnL.
    pub fn realized_pnl(&self) -> Decimal18 {
        self.snapshot().realized_pnl()
    }

    /// Aggregate attributed unrealized PnL when marked.
    pub fn unrealized_pnl(&self) -> Option<Decimal18> {
        self.snapshot().unrealized_pnl()
    }

    /// Aggregate marked equity.
    pub fn equity(&self) -> Option<Decimal18> {
        self.snapshot().equity()
    }

    /// Aggregate marked change in equity.
    pub fn total_pnl(&self) -> Option<Decimal18> {
        self.snapshot().total_pnl()
    }

    /// Register an existing virtual book after validating global fill IDs.
    pub fn register_book(&mut self, book: VirtualBook) -> LiveResult<()> {
        if self.book_index.contains_key(book.book_id()) {
            return Err(LiveError::Portfolio(format!(
                "book {:?} is already registered",
                book.book_id()
            )));
        }
        for fill_id in book.fill_ids() {
            if let Some(routed_book) = self.fill_routes.get(fill_id) {
                return Err(LiveError::Portfolio(format!(
                    "fill {fill_id:?} is already routed to book {routed_book:?}"
                )));
            }
        }
        let mut book = book;
        for marker in self.markers.values() {
            book.apply_marker(marker.clone());
        }
        let index = self.books.len();
        for fill_id in book.fill_ids() {
            self.fill_routes
                .insert(fill_id.clone(), book.book_id().to_string());
        }
        self.book_index.insert(book.book_id().to_string(), index);
        self.books.push(book);
        Ok(())
    }

    /// Create and register a strategy virtual book.
    pub fn create_book(&mut self, book_id: &str, initial_cash: Decimal18) -> LiveResult<()> {
        self.register_book(VirtualBook::new(book_id, initial_cash)?)
    }

    /// Return a registered immutable virtual book.
    pub fn book(&self, book_id: &str) -> LiveResult<&VirtualBook> {
        let index = self
            .book_index
            .get(book_id)
            .ok_or_else(|| LiveError::Portfolio(format!("unknown book {book_id:?}")))?;
        Ok(&self.books[*index])
    }

    /// Route one globally unique fill to exactly one strategy book.
    pub fn apply_fill(&mut self, book_id: &str, fill: &Fill) -> LiveResult<PositionSnapshot> {
        if let Some(routed_book) = self.fill_routes.get(&fill.fill_id) {
            return Err(LiveError::Portfolio(format!(
                "fill {:?} is already routed to book {routed_book:?}",
                fill.fill_id
            )));
        }
        let index = *self
            .book_index
            .get(book_id)
            .ok_or_else(|| LiveError::Portfolio(format!("unknown book {book_id:?}")))?;
        let position = self.books[index].apply_fill(fill)?;
        self.fill_routes
            .insert(fill.fill_id.clone(), book_id.to_string());
        Ok(position)
    }

    /// Retire one expired contract ledger at its final settlement value.
    pub fn retire_position(
        &mut self,
        book_id: &str,
        symbol: &str,
        settlement_value: Decimal18,
    ) -> LiveResult<PositionSnapshot> {
        let index = *self
            .book_index
            .get(book_id)
            .ok_or_else(|| LiveError::Portfolio(format!("unknown book {book_id:?}")))?;
        self.books[index].retire_position(symbol, settlement_value)
    }

    /// Apply one explicit market marker to every strategy book.
    pub fn apply_marker(&mut self, marker: PositionMarker) -> PositionMarker {
        for book in &mut self.books {
            book.apply_marker(marker.clone());
        }
        self.markers.insert(marker.symbol.clone(), marker.clone());
        marker
    }

    /// Create and broadcast an explicit price marker.
    pub fn mark(&mut self, symbol: &str, price: Decimal18) -> LiveResult<PositionMarker> {
        let marker = PositionMarker::new(canonical_symbol(symbol)?, price)?;
        Ok(self.apply_marker(marker))
    }
}

fn aggregate_position(symbol: &Symbol, positions: &[PositionSnapshot]) -> AggregatePosition {
    let mut quantity = ZERO;
    let mut long_quantity = ZERO;
    let mut short_quantity = ZERO;
    let mut cost_basis = ZERO;
    let mut cash_flow = ZERO;
    let mut realized_pnl = ZERO;
    for position in positions {
        quantity += position.quantity;
        if position.quantity.is_positive() {
            long_quantity += position.quantity;
        } else if position.quantity.is_negative() {
            short_quantity += -position.quantity;
        }
        cost_basis += position.cost_basis;
        cash_flow += position.cash_flow;
        realized_pnl += position.realized_pnl;
    }
    let average_cost = if quantity != ZERO && (long_quantity == ZERO || short_quantity == ZERO) {
        Some(cost_basis / quantity)
    } else {
        None
    };

    let open_positions: Vec<&PositionSnapshot> = positions
        .iter()
        .filter(|position| position.quantity != ZERO)
        .collect();
    let mut marks: Vec<Option<Decimal18>> = open_positions
        .iter()
        .map(|position| position.mark_price)
        .collect();
    marks.sort();
    marks.dedup();
    let mark_price = if marks.len() == 1 && marks[0].is_some() {
        marks[0]
    } else {
        None
    };

    let mut market_value: Option<Decimal18> = Some(ZERO);
    for position in positions {
        match position.market_value() {
            None => {
                market_value = None;
                break;
            }
            Some(value) => market_value = market_value.map(|sum| sum + value),
        }
    }
    let unrealized_pnl = market_value.map(|value| cash_flow + value - realized_pnl);

    AggregatePosition {
        symbol: symbol.clone(),
        quantity,
        long_quantity,
        short_quantity,
        cost_basis,
        cash_flow,
        average_cost,
        realized_pnl,
        mark_price,
        market_value,
        unrealized_pnl,
    }
}

#[cfg(test)]
mod tests {
    use harness_num::Decimal18;

    use super::PortfolioHub;
    use crate::accounting::Fill;
    use crate::orders::Side;
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    fn fill(fill_id: &str, side: Side, quantity: &str, price: &str) -> Fill {
        Fill::new(
            fill_id,
            "o1",
            Symbol::new("AAPL").unwrap(),
            side,
            dec(quantity),
            dec(price),
            Decimal18::ZERO,
            None,
        )
        .unwrap()
    }

    #[test]
    fn hub_aggregates_books_and_retains_attribution() {
        let mut hub = PortfolioHub::new("acct", vec![]).unwrap();
        hub.create_book("a", dec("1000")).unwrap();
        hub.create_book("b", dec("500")).unwrap();
        hub.apply_fill("a", &fill("f1", Side::Buy, "10", "100"))
            .unwrap();
        hub.apply_fill("b", &fill("f2", Side::Sell, "4", "110"))
            .unwrap();
        let snapshot = hub.snapshot();
        assert_eq!(snapshot.initial_cash, dec("1500"));
        assert_eq!(snapshot.positions.len(), 1);
        assert_eq!(snapshot.positions[0].quantity, dec("6"));
        assert_eq!(snapshot.positions[0].long_quantity, dec("10"));
        assert_eq!(snapshot.positions[0].short_quantity, dec("4"));
    }

    #[test]
    fn global_fill_ids_cannot_be_reused_across_books() {
        let mut hub = PortfolioHub::new("acct", vec![]).unwrap();
        hub.create_book("a", dec("1000")).unwrap();
        hub.create_book("b", dec("1000")).unwrap();
        hub.apply_fill("a", &fill("f1", Side::Buy, "1", "100"))
            .unwrap();
        assert!(hub
            .apply_fill("b", &fill("f1", Side::Buy, "1", "100"))
            .is_err());
    }

    #[test]
    fn hub_broadcasts_marks_to_every_book() {
        let mut hub = PortfolioHub::new("acct", vec![]).unwrap();
        hub.create_book("a", dec("1000")).unwrap();
        hub.create_book("b", dec("1000")).unwrap();
        hub.apply_fill("a", &fill("f1", Side::Buy, "1", "100"))
            .unwrap();
        hub.mark("AAPL", dec("110")).unwrap();
        assert_eq!(
            hub.book("b")
                .unwrap()
                .marker("AAPL")
                .unwrap()
                .unwrap()
                .price,
            dec("110")
        );
        assert_eq!(hub.equity(), Some(dec("2010")));
    }
}
