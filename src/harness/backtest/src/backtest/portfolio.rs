//! Decimal-exact accounting for virtual books and the shared portfolio hub.
//!
//! Port of `harness.portfolio.book` and `harness.portfolio.hub` using
//! [`Decimal`] so every rendered value keeps the exact canonical text.

use std::collections::BTreeMap;

use num::Decimal;

use crate::canonical_json::JsonValue;

/// One applied fill input for accounting.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BookFill {
    /// Fill identifier.
    pub fill_id: String,
    /// `buy` or `sell`.
    pub side: String,
    /// Signed quantity as an exact decimal.
    pub quantity: Decimal,
    /// Execution price.
    pub price: Decimal,
    /// Fee recognized immediately.
    pub fee: Decimal,
    /// Instrument symbol.
    pub symbol: String,
}

#[derive(Clone, Debug)]
struct PositionLedger {
    quantity: Decimal,
    cost_basis: Decimal,
    cash_flow: Decimal,
    realized_pnl: Decimal,
}

impl PositionLedger {}

/// Immutable valuation of one strategy virtual book.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BookSnapshot {
    /// Book identifier.
    pub book_id: String,
    /// Opening cash.
    pub initial_cash: Decimal,
    /// Current cash.
    pub cash: Decimal,
    /// Position snapshots in canonical order.
    pub positions: Vec<PositionSnapshot>,
    /// Fill count.
    pub fill_count: usize,
}

/// Immutable signed position, cost basis, and attributed PnL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PositionSnapshot {
    /// Symbol.
    pub symbol: String,
    /// Signed quantity.
    pub quantity: Decimal,
    /// Cost basis.
    pub cost_basis: Decimal,
    /// Attributed cash flow.
    pub cash_flow: Decimal,
    /// Realized PnL.
    pub realized_pnl: Decimal,
    /// Mark price, if a marker exists.
    pub mark_price: Option<Decimal>,
}

impl PositionSnapshot {
    /// Whether the position is flat.
    #[must_use]
    pub fn is_flat(&self) -> bool {
        self.quantity.is_zero()
    }

    /// Average entry price, or zero when flat.
    #[must_use]
    pub fn average_cost(&self) -> Decimal {
        if self.is_flat() {
            return Decimal::ZERO;
        }
        self.cost_basis
            .checked_div(self.quantity)
            .unwrap_or(Decimal::ZERO)
    }

    /// Signed marked value; zero when flat, `None` when open and unmarked.
    #[must_use]
    pub fn market_value(&self) -> Option<Decimal> {
        if self.is_flat() {
            return Some(Decimal::ZERO);
        }
        self.mark_price
            .map(|mark| self.quantity.checked_mul(mark).unwrap_or(Decimal::ZERO))
    }

    /// Realized plus unrealized PnL, when marked.
    #[must_use]
    pub fn total_pnl(&self) -> Option<Decimal> {
        self.market_value()
            .map(|value| self.cash_flow.checked_add(value).unwrap_or(self.cash_flow))
    }

    /// Unrealized PnL as the remainder after realized PnL, when marked.
    #[must_use]
    pub fn unrealized_pnl(&self) -> Option<Decimal> {
        self.total_pnl().map(|total| {
            total
                .checked_sub(self.realized_pnl)
                .unwrap_or(Decimal::ZERO)
        })
    }
}

impl BookSnapshot {
    /// Realized PnL across all positions.
    #[must_use]
    pub fn realized_pnl(&self) -> Decimal {
        let mut total = Decimal::ZERO;
        for position in &self.positions {
            total = total.checked_add(position.realized_pnl).unwrap_or(total);
        }
        total
    }

    /// Total signed market value when every open leg is marked.
    #[must_use]
    pub fn market_value(&self) -> Option<Decimal> {
        let mut total = Decimal::ZERO;
        for position in &self.positions {
            let value = position.market_value()?;
            total = total.checked_add(value).unwrap_or(total);
        }
        Some(total)
    }

    /// Marked book equity.
    #[must_use]
    pub fn equity(&self) -> Option<Decimal> {
        self.market_value()
            .map(|value| self.cash.checked_add(value).unwrap_or(self.cash))
    }

    /// Unrealized PnL when all open legs are marked.
    #[must_use]
    pub fn unrealized_pnl(&self) -> Option<Decimal> {
        self.equity()
            .map(|equity| {
                equity
                    .checked_sub(self.initial_cash)
                    .unwrap_or(Decimal::ZERO)
            })
            .and_then(|total_pnl| total_pnl.checked_sub(self.realized_pnl()).ok())
    }

    /// Total PnL as the marked change in equity.
    #[must_use]
    pub fn total_pnl(&self) -> Option<Decimal> {
        self.equity().map(|equity| {
            equity
                .checked_sub(self.initial_cash)
                .unwrap_or(Decimal::ZERO)
        })
    }
}

/// Maintain cash and average-cost positions for one strategy.
#[derive(Clone, Debug)]
pub struct VirtualBook {
    /// Book identifier.
    pub book_id: String,
    initial_cash: Decimal,
    cash: Decimal,
    positions: BTreeMap<String, PositionLedger>,
    markers: BTreeMap<String, Decimal>,
    fill_ids: Vec<String>,
}

impl VirtualBook {
    /// Create a book with the given opening cash.
    #[must_use]
    pub fn new(book_id: impl Into<String>, initial_cash: Decimal) -> Self {
        Self {
            book_id: book_id.into(),
            initial_cash,
            cash: initial_cash,
            positions: BTreeMap::new(),
            markers: BTreeMap::new(),
            fill_ids: Vec::new(),
        }
    }

    /// Opening cash.
    #[must_use]
    pub const fn initial_cash(&self) -> Decimal {
        self.initial_cash
    }

    /// Current cash.
    #[must_use]
    pub const fn cash(&self) -> Decimal {
        self.cash
    }

    /// Number of accounted fills.
    #[must_use]
    pub fn fill_count(&self) -> usize {
        self.fill_ids.len()
    }

    /// Current immutable valuation.
    #[must_use]
    pub fn snapshot(&self) -> BookSnapshot {
        let positions = self
            .positions
            .iter()
            .map(|(symbol, ledger)| self.position_snapshot(symbol, ledger))
            .collect();
        BookSnapshot {
            book_id: self.book_id.clone(),
            initial_cash: self.initial_cash,
            cash: self.cash,
            positions,
            fill_count: self.fill_count(),
        }
    }

    /// Account one fill exactly.
    pub fn apply_fill(&mut self, fill: &BookFill) {
        let buy = fill.side == "buy";
        let signed_quantity = if buy {
            fill.quantity
        } else {
            fill.quantity.negated()
        };
        let current = self.positions.get(&fill.symbol).cloned();
        let (quantity, mut cost_basis, mut cash_flow, mut realized_pnl) = match &current {
            Some(ledger) => (
                ledger.quantity,
                ledger.cost_basis,
                ledger.cash_flow,
                ledger.realized_pnl,
            ),
            None => (Decimal::ZERO, Decimal::ZERO, Decimal::ZERO, Decimal::ZERO),
        };
        let next_quantity = quantity.checked_add(signed_quantity).unwrap_or(quantity);

        if quantity.is_zero()
            || quantity
                .checked_mul(signed_quantity)
                .is_ok_and(|v| v.is_positive())
        {
            let addition = signed_quantity
                .checked_mul(fill.price)
                .unwrap_or(Decimal::ZERO);
            cost_basis = cost_basis.checked_add(addition).unwrap_or(cost_basis);
        } else if abs_le(signed_quantity, quantity) {
            let released_basis = if abs_eq(signed_quantity, quantity) {
                cost_basis
            } else {
                cost_basis
                    .checked_mul(signed_quantity.abs())
                    .and_then(|value| value.checked_div(quantity.abs()))
                    .unwrap_or(Decimal::ZERO)
            };
            let term = signed_quantity
                .negated()
                .checked_mul(fill.price)
                .unwrap_or(Decimal::ZERO)
                .checked_sub(released_basis)
                .unwrap_or(Decimal::ZERO);
            realized_pnl = realized_pnl.checked_add(term).unwrap_or(realized_pnl);
            cost_basis = cost_basis.checked_sub(released_basis).unwrap_or(cost_basis);
        } else {
            let closing_quantity = quantity.negated();
            let term = closing_quantity
                .negated()
                .checked_mul(fill.price)
                .unwrap_or(Decimal::ZERO)
                .checked_sub(cost_basis)
                .unwrap_or(Decimal::ZERO);
            realized_pnl = realized_pnl.checked_add(term).unwrap_or(realized_pnl);
            cost_basis = next_quantity
                .checked_mul(fill.price)
                .unwrap_or(Decimal::ZERO);
        }

        realized_pnl = realized_pnl.checked_sub(fill.fee).unwrap_or(realized_pnl);
        let notional = signed_quantity
            .checked_mul(fill.price)
            .unwrap_or(Decimal::ZERO);
        cash_flow = cash_flow
            .checked_sub(notional)
            .and_then(|value| value.checked_sub(fill.fee))
            .unwrap_or(cash_flow);
        if next_quantity.is_zero() {
            cost_basis = Decimal::ZERO;
            realized_pnl = cash_flow;
        }
        let next_cash = self
            .cash
            .checked_sub(notional)
            .and_then(|value| value.checked_sub(fill.fee))
            .unwrap_or(self.cash);
        self.positions.insert(
            fill.symbol.clone(),
            PositionLedger {
                quantity: next_quantity,
                cost_basis,
                cash_flow,
                realized_pnl,
            },
        );
        self.cash = next_cash;
        self.fill_ids.push(fill.fill_id.clone());
    }

    /// Apply one explicit mark.
    pub fn mark(&mut self, symbol: &str, price: Decimal) {
        self.markers.insert(symbol.to_string(), price);
    }

    fn position_snapshot(&self, symbol: &str, ledger: &PositionLedger) -> PositionSnapshot {
        PositionSnapshot {
            symbol: symbol.to_string(),
            quantity: ledger.quantity,
            cost_basis: ledger.cost_basis,
            cash_flow: ledger.cash_flow,
            realized_pnl: ledger.realized_pnl,
            mark_price: self.markers.get(symbol).copied(),
        }
    }
}

/// Immutable shared-account aggregate and constituent book snapshots.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PortfolioSnapshot {
    /// Account identifier.
    pub account_id: String,
    /// Total opening cash.
    pub initial_cash: Decimal,
    /// Total cash.
    pub cash: Decimal,
    /// Aggregated positions.
    pub positions: Vec<AggregatePosition>,
    /// Constituent book snapshots.
    pub books: Vec<BookSnapshot>,
    /// Total fill count.
    pub fill_count: usize,
}

/// Shared-account totals retaining virtual-book PnL attribution.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AggregatePosition {
    /// Symbol.
    pub symbol: String,
    /// Net quantity.
    pub quantity: Decimal,
    /// Long quantity.
    pub long_quantity: Decimal,
    /// Absolute short quantity.
    pub short_quantity: Decimal,
    /// Aggregate cost basis.
    pub cost_basis: Decimal,
    /// Aggregate cash flow.
    pub cash_flow: Decimal,
    /// Average cost when the aggregate is one-directional.
    pub average_cost: Option<Decimal>,
    /// Realized PnL.
    pub realized_pnl: Decimal,
    /// Common mark when exactly one distinct non-null mark exists.
    pub mark_price: Option<Decimal>,
    /// Marked market value.
    pub market_value: Option<Decimal>,
    /// Unrealized PnL.
    pub unrealized_pnl: Option<Decimal>,
}

impl PortfolioSnapshot {
    /// Realized PnL attributed across all books.
    #[must_use]
    pub fn realized_pnl(&self) -> Decimal {
        let mut total = Decimal::ZERO;
        for position in &self.positions {
            total = total.checked_add(position.realized_pnl).unwrap_or(total);
        }
        total
    }

    /// Total signed market value when every open leg is marked.
    #[must_use]
    pub fn market_value(&self) -> Option<Decimal> {
        let mut total = Decimal::ZERO;
        for position in &self.positions {
            let value = position.market_value?;
            total = total.checked_add(value).unwrap_or(total);
        }
        Some(total)
    }

    /// Shared marked equity.
    #[must_use]
    pub fn equity(&self) -> Option<Decimal> {
        self.market_value()
            .map(|value| self.cash.checked_add(value).unwrap_or(self.cash))
    }

    /// Total PnL as the marked change in aggregate equity.
    #[must_use]
    pub fn total_pnl(&self) -> Option<Decimal> {
        self.equity().map(|equity| {
            equity
                .checked_sub(self.initial_cash)
                .unwrap_or(Decimal::ZERO)
        })
    }

    /// Unrealized PnL when every open leg is marked.
    #[must_use]
    pub fn unrealized_pnl(&self) -> Option<Decimal> {
        let total = self.total_pnl()?;
        total.checked_sub(self.realized_pnl()).ok()
    }
}

/// Route fills to independent books and expose shared-account totals.
#[derive(Clone, Debug)]
pub struct PortfolioHub {
    /// Account identifier.
    pub account_id: String,
    books: Vec<VirtualBook>,
}

impl PortfolioHub {
    /// Create an empty hub.
    #[must_use]
    pub fn new(account_id: impl Into<String>) -> Self {
        Self {
            account_id: account_id.into(),
            books: Vec::new(),
        }
    }

    /// Create and register a strategy book.
    pub fn create_book(&mut self, book_id: impl Into<String>, initial_cash: Decimal) {
        self.books.push(VirtualBook::new(book_id, initial_cash));
    }

    /// Borrow a mutable book.
    pub fn book_mut(&mut self, book_id: &str) -> Option<&mut VirtualBook> {
        self.books.iter_mut().find(|book| book.book_id == book_id)
    }

    /// Borrow a book.
    #[must_use]
    pub fn book(&self, book_id: &str) -> Option<&VirtualBook> {
        self.books.iter().find(|book| book.book_id == book_id)
    }

    /// Route one fill to exactly one strategy book.
    pub fn apply_fill(&mut self, book_id: &str, fill: &BookFill) {
        if let Some(book) = self.book_mut(book_id) {
            book.apply_fill(fill);
        }
    }

    /// Broadcast one explicit mark to every book.
    pub fn mark(&mut self, symbol: &str, price: Decimal) {
        for book in &mut self.books {
            book.mark(symbol, price);
        }
    }

    /// Current immutable valuation.
    #[must_use]
    pub fn snapshot(&self) -> PortfolioSnapshot {
        let books: Vec<BookSnapshot> = self.books.iter().map(VirtualBook::snapshot).collect();
        let mut grouped: BTreeMap<String, Vec<PositionSnapshot>> = BTreeMap::new();
        for book in &books {
            for position in &book.positions {
                grouped
                    .entry(position.symbol.clone())
                    .or_default()
                    .push(position.clone());
            }
        }
        let mut initial_cash = Decimal::ZERO;
        let mut cash = Decimal::ZERO;
        let mut fill_count = 0usize;
        for book in &books {
            initial_cash = initial_cash
                .checked_add(book.initial_cash)
                .unwrap_or(initial_cash);
            cash = cash.checked_add(book.cash).unwrap_or(cash);
            fill_count += book.fill_count;
        }
        let positions = grouped
            .into_iter()
            .map(|(symbol, entries)| aggregate_position(&symbol, &entries))
            .collect();
        PortfolioSnapshot {
            account_id: self.account_id.clone(),
            initial_cash,
            cash,
            positions,
            books,
            fill_count,
        }
    }
}

fn aggregate_position(symbol: &str, positions: &[PositionSnapshot]) -> AggregatePosition {
    let mut quantity = Decimal::ZERO;
    let mut long_quantity = Decimal::ZERO;
    let mut short_quantity = Decimal::ZERO;
    let mut cost_basis = Decimal::ZERO;
    let mut cash_flow = Decimal::ZERO;
    let mut realized_pnl = Decimal::ZERO;
    for position in positions {
        quantity = quantity.checked_add(position.quantity).unwrap_or(quantity);
        if position.quantity.is_positive() {
            long_quantity = long_quantity
                .checked_add(position.quantity)
                .unwrap_or(long_quantity);
        } else if position.quantity.is_negative() {
            short_quantity = short_quantity
                .checked_add(position.quantity.negated())
                .unwrap_or(short_quantity);
        }
        cost_basis = cost_basis
            .checked_add(position.cost_basis)
            .unwrap_or(cost_basis);
        cash_flow = cash_flow
            .checked_add(position.cash_flow)
            .unwrap_or(cash_flow);
        realized_pnl = realized_pnl
            .checked_add(position.realized_pnl)
            .unwrap_or(realized_pnl);
    }
    let average_cost =
        if !quantity.is_zero() && (long_quantity.is_zero() || short_quantity.is_zero()) {
            cost_basis.checked_div(quantity).ok()
        } else {
            None
        };
    let mut marks: Vec<Decimal> = Vec::new();
    let mut saw_none = false;
    for position in positions.iter().filter(|p| !p.quantity.is_zero()) {
        match position.mark_price {
            Some(mark) => {
                if !marks
                    .iter()
                    .any(|existing| existing.cmp_value(mark) == std::cmp::Ordering::Equal)
                {
                    marks.push(mark);
                }
            }
            None => saw_none = true,
        }
    }
    let mark_price = if marks.len() == 1 && !saw_none {
        Some(marks[0])
    } else {
        None
    };
    let mut market_value: Option<Decimal> = Some(Decimal::ZERO);
    for position in positions {
        match position.market_value() {
            Some(value) => {
                market_value = market_value.map(|total| total.checked_add(value).unwrap_or(total));
            }
            None => {
                market_value = None;
                break;
            }
        }
    }
    let unrealized_pnl = market_value.map(|value| {
        cash_flow
            .checked_add(value)
            .unwrap_or(cash_flow)
            .checked_sub(realized_pnl)
            .unwrap_or(Decimal::ZERO)
    });
    AggregatePosition {
        symbol: symbol.to_string(),
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

fn abs_eq(left: Decimal, right: Decimal) -> bool {
    left.abs().cmp_value(right.abs()) == std::cmp::Ordering::Equal
}

fn abs_le(left: Decimal, right: Decimal) -> bool {
    left.abs().cmp_value(right.abs()) != std::cmp::Ordering::Greater
}

/// Render an optional decimal as a JSON string or null (Python `_decimal_or_none`).
pub(crate) fn decimal_or_none(value: Option<Decimal>) -> JsonValue {
    match value {
        Some(value) => JsonValue::String(value.to_string()),
        None => JsonValue::Null,
    }
}

/// Render a portfolio snapshot exactly as `manifest._portfolio_json`.
#[must_use]
pub fn portfolio_json(snapshot: &PortfolioSnapshot) -> JsonValue {
    JsonValue::object([
        ("account_id", JsonValue::String(snapshot.account_id.clone())),
        (
            "initial_cash",
            JsonValue::String(snapshot.initial_cash.to_string()),
        ),
        ("cash", JsonValue::String(snapshot.cash.to_string())),
        ("fill_count", JsonValue::UInt(snapshot.fill_count as u64)),
        (
            "realized_pnl",
            JsonValue::String(snapshot.realized_pnl().to_string()),
        ),
        ("unrealized_pnl", decimal_or_none(snapshot.unrealized_pnl())),
        ("equity", decimal_or_none(snapshot.equity())),
        ("total_pnl", decimal_or_none(snapshot.total_pnl())),
        (
            "positions",
            JsonValue::Array(
                snapshot
                    .positions
                    .iter()
                    .map(aggregate_position_json)
                    .collect(),
            ),
        ),
        (
            "books",
            JsonValue::Array(snapshot.books.iter().map(book_json).collect()),
        ),
    ])
}

fn aggregate_position_json(position: &AggregatePosition) -> JsonValue {
    JsonValue::object([
        ("symbol", JsonValue::String(position.symbol.clone())),
        ("quantity", JsonValue::String(position.quantity.to_string())),
        (
            "long_quantity",
            JsonValue::String(position.long_quantity.to_string()),
        ),
        (
            "short_quantity",
            JsonValue::String(position.short_quantity.to_string()),
        ),
        (
            "cost_basis",
            JsonValue::String(position.cost_basis.to_string()),
        ),
        (
            "cash_flow",
            JsonValue::String(position.cash_flow.to_string()),
        ),
        ("average_cost", decimal_or_none(position.average_cost)),
        (
            "realized_pnl",
            JsonValue::String(position.realized_pnl.to_string()),
        ),
        ("mark_price", decimal_or_none(position.mark_price)),
        ("market_value", decimal_or_none(position.market_value)),
        ("unrealized_pnl", decimal_or_none(position.unrealized_pnl)),
    ])
}

fn book_json(book: &BookSnapshot) -> JsonValue {
    JsonValue::object([
        ("book_id", JsonValue::String(book.book_id.clone())),
        (
            "initial_cash",
            JsonValue::String(book.initial_cash.to_string()),
        ),
        ("cash", JsonValue::String(book.cash.to_string())),
        ("equity", decimal_or_none(book.equity())),
        (
            "realized_pnl",
            JsonValue::String(book.realized_pnl().to_string()),
        ),
        ("fill_count", JsonValue::UInt(book.fill_count as u64)),
        (
            "positions",
            JsonValue::Array(book.positions.iter().map(book_position_json).collect()),
        ),
    ])
}

fn book_position_json(position: &PositionSnapshot) -> JsonValue {
    JsonValue::object([
        ("symbol", JsonValue::String(position.symbol.clone())),
        ("quantity", JsonValue::String(position.quantity.to_string())),
        (
            "cost_basis",
            JsonValue::String(position.cost_basis.to_string()),
        ),
        (
            "cash_flow",
            JsonValue::String(position.cash_flow.to_string()),
        ),
        (
            "average_cost",
            decimal_or_none(Some(position.average_cost())),
        ),
        (
            "realized_pnl",
            JsonValue::String(position.realized_pnl.to_string()),
        ),
        ("mark_price", decimal_or_none(position.mark_price)),
        ("market_value", decimal_or_none(position.market_value())),
    ])
}
