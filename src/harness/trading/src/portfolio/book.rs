//! Decimal-exact accounting for one strategy's virtual book.

use std::collections::HashMap;
use std::collections::HashSet;

use num::Decimal18;

use crate::accounting::Fill;
use crate::error::require_text;
use crate::error::LiveError;
use crate::error::LiveResult;
use crate::orders::Side;
use crate::symbol::canonical_symbol;
use crate::symbol::Symbol;

const ZERO: Decimal18 = Decimal18::ZERO;

/// An explicit mark used to value a position without consulting a clock.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PositionMarker {
    /// The marked symbol.
    pub symbol: Symbol,
    /// The positive mark price.
    pub price: Decimal18,
}

impl PositionMarker {
    /// Construct a validated marker.
    pub fn new(symbol: Symbol, price: Decimal18) -> LiveResult<Self> {
        if !price.is_positive() {
            return Err(LiveError::Portfolio("price must be positive".into()));
        }
        Ok(Self { symbol, price })
    }
}

/// Immutable signed position, cost basis, and attributed PnL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PositionSnapshot {
    /// The position symbol.
    pub symbol: Symbol,
    /// Signed quantity.
    pub quantity: Decimal18,
    /// Total cost basis.
    pub cost_basis: Decimal18,
    /// Cumulative cash flow including fees.
    pub cash_flow: Decimal18,
    /// Cumulative realized PnL.
    pub realized_pnl: Decimal18,
    /// The current explicit mark.
    pub mark_price: Option<Decimal18>,
}

impl PositionSnapshot {
    /// Whether no position is currently open.
    pub fn is_flat(&self) -> bool {
        self.quantity == ZERO
    }

    /// The average entry price, or zero for a flat position.
    pub fn average_cost(&self) -> Decimal18 {
        if self.is_flat() {
            ZERO
        } else {
            self.cost_basis / self.quantity
        }
    }

    /// Signed marked value, requiring a mark only when open.
    pub fn market_value(&self) -> Option<Decimal18> {
        if self.is_flat() {
            return Some(ZERO);
        }
        self.mark_price.map(|price| self.quantity * price)
    }

    /// Marked PnL as the exact remainder after realized PnL.
    pub fn unrealized_pnl(&self) -> Option<Decimal18> {
        self.total_pnl().map(|total| total - self.realized_pnl)
    }

    /// The exact marked change from this position's cash flows.
    pub fn total_pnl(&self) -> Option<Decimal18> {
        self.market_value().map(|value| self.cash_flow + value)
    }
}

/// Immutable valuation of one strategy virtual book.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BookSnapshot {
    /// The book identifier.
    pub book_id: String,
    /// Opening cash allocation.
    pub initial_cash: Decimal18,
    /// Current cash.
    pub cash: Decimal18,
    /// Position snapshots in canonical symbol order.
    pub positions: Vec<PositionSnapshot>,
    /// Explicit markers in canonical symbol order.
    pub markers: Vec<PositionMarker>,
    /// Number of accounted fills.
    pub fill_count: usize,
}

impl BookSnapshot {
    /// Realized PnL across all symbols, including all fill fees.
    pub fn realized_pnl(&self) -> Decimal18 {
        self.positions
            .iter()
            .fold(ZERO, |sum, position| sum + position.realized_pnl)
    }

    /// Total unrealized PnL, or `None` if an open leg is unmarked.
    pub fn unrealized_pnl(&self) -> Option<Decimal18> {
        self.total_pnl().map(|total| total - self.realized_pnl())
    }

    /// Total signed market value when every open leg is marked.
    pub fn market_value(&self) -> Option<Decimal18> {
        let mut total = ZERO;
        for position in &self.positions {
            total += position.market_value()?;
        }
        Some(total)
    }

    /// Marked book equity, or `None` while a leg is unmarked.
    pub fn equity(&self) -> Option<Decimal18> {
        self.market_value().map(|value| self.cash + value)
    }

    /// The exact marked change in book equity.
    pub fn total_pnl(&self) -> Option<Decimal18> {
        self.equity().map(|equity| equity - self.initial_cash)
    }

    /// Return one position from this immutable snapshot.
    pub fn position(&self, symbol: &str) -> LiveResult<PositionSnapshot> {
        let canonical = canonical_symbol(symbol)?;
        self.positions
            .iter()
            .find(|position| position.symbol == canonical)
            .cloned()
            .ok_or_else(|| LiveError::Portfolio(format!("unknown position {symbol:?}")))
    }
}

#[derive(Clone, Debug)]
struct PositionLedger {
    quantity: Decimal18,
    cost_basis: Decimal18,
    cash_flow: Decimal18,
    realized_pnl: Decimal18,
}

/// Maintain cash and average-cost positions for one strategy.
#[derive(Clone, Debug)]
pub struct VirtualBook {
    book_id: String,
    initial_cash: Decimal18,
    cash: Decimal18,
    positions: HashMap<Symbol, PositionLedger>,
    markers: HashMap<Symbol, PositionMarker>,
    fill_ids: Vec<String>,
    seen_fill_ids: HashSet<String>,
}

impl VirtualBook {
    /// Construct a book with an opening cash allocation.
    pub fn new(book_id: &str, initial_cash: Decimal18) -> LiveResult<Self> {
        Ok(Self {
            book_id: require_text(book_id, "book_id")?,
            initial_cash,
            cash: initial_cash,
            positions: HashMap::new(),
            markers: HashMap::new(),
            fill_ids: Vec::new(),
            seen_fill_ids: HashSet::new(),
        })
    }

    /// The book identifier.
    pub fn book_id(&self) -> &str {
        &self.book_id
    }

    /// The opening cash allocation.
    pub fn initial_cash(&self) -> Decimal18 {
        self.initial_cash
    }

    /// Current exact cash after notional and fees.
    pub fn cash(&self) -> Decimal18 {
        self.cash
    }

    /// Accounted fill identifiers in application order.
    pub fn fill_ids(&self) -> &[String] {
        &self.fill_ids
    }

    /// The number of accounted fills.
    pub fn fill_count(&self) -> usize {
        self.fill_ids.len()
    }

    /// Current position snapshots in canonical symbol order.
    pub fn positions(&self) -> Vec<PositionSnapshot> {
        self.snapshot().positions
    }

    /// The current immutable book valuation.
    pub fn snapshot(&self) -> BookSnapshot {
        let mut symbols: Vec<&Symbol> = self.positions.keys().collect();
        symbols.sort();
        let positions = symbols
            .into_iter()
            .map(|symbol| self.position_snapshot(symbol, &self.positions[symbol]))
            .collect();
        let mut marker_symbols: Vec<&Symbol> = self.markers.keys().collect();
        marker_symbols.sort();
        let markers = marker_symbols
            .into_iter()
            .map(|symbol| self.markers[symbol].clone())
            .collect();
        BookSnapshot {
            book_id: self.book_id.clone(),
            initial_cash: self.initial_cash,
            cash: self.cash,
            positions,
            markers,
            fill_count: self.fill_count(),
        }
    }

    /// Realized PnL across all positions.
    pub fn realized_pnl(&self) -> Decimal18 {
        self.snapshot().realized_pnl()
    }

    /// Unrealized PnL when all open positions are marked.
    pub fn unrealized_pnl(&self) -> Option<Decimal18> {
        self.snapshot().unrealized_pnl()
    }

    /// Marked strategy equity.
    pub fn equity(&self) -> Option<Decimal18> {
        self.snapshot().equity()
    }

    /// The marked change in strategy equity.
    pub fn total_pnl(&self) -> Option<Decimal18> {
        self.snapshot().total_pnl()
    }

    /// Atomically account one immutable core fill.
    pub fn apply_fill(&mut self, fill: &Fill) -> LiveResult<PositionSnapshot> {
        if self.seen_fill_ids.contains(&fill.fill_id) {
            return Err(LiveError::Portfolio(format!(
                "fill {:?} was already applied",
                fill.fill_id
            )));
        }
        let signed_quantity = if fill.side == Side::Buy {
            fill.quantity
        } else {
            -fill.quantity
        };
        let current = self.positions.get(&fill.symbol).cloned();
        let quantity = current.as_ref().map(|entry| entry.quantity).unwrap_or(ZERO);
        let cost_basis = current
            .as_ref()
            .map(|entry| entry.cost_basis)
            .unwrap_or(ZERO);
        let cash_flow = current
            .as_ref()
            .map(|entry| entry.cash_flow)
            .unwrap_or(ZERO);
        let realized_pnl = current
            .as_ref()
            .map(|entry| entry.realized_pnl)
            .unwrap_or(ZERO);
        let (next_cash, next) = crate::ledger::apply(
            self.cash,
            crate::ledger::Position {
                quantity,
                cost_basis,
                cash_flow,
                realized_pnl,
            },
            signed_quantity,
            fill.price,
            fill.fee,
        )
        .map_err(|error| LiveError::Portfolio(error.to_string()))?;
        let next_ledger = PositionLedger {
            quantity: next.quantity,
            cost_basis: next.cost_basis,
            cash_flow: next.cash_flow,
            realized_pnl: next.realized_pnl,
        };
        self.cash = next_cash;
        self.positions
            .insert(fill.symbol.clone(), next_ledger.clone());
        self.fill_ids.push(fill.fill_id.clone());
        self.seen_fill_ids.insert(fill.fill_id.clone());
        Ok(self.position_snapshot(&fill.symbol, &next_ledger))
    }

    /// Retire one expired contract ledger at its final settlement value.
    pub fn retire_position(
        &mut self,
        symbol: &str,
        settlement_value: Decimal18,
    ) -> LiveResult<PositionSnapshot> {
        let canonical = canonical_symbol(symbol)?;
        let Some(ledger) = self.positions.get(&canonical) else {
            return Err(LiveError::Portfolio(format!("unknown position {symbol:?}")));
        };
        if ledger.quantity == ZERO {
            return Err(LiveError::Portfolio(format!(
                "position {symbol:?} is already flat"
            )));
        }
        let next_cash_flow = ledger.cash_flow + settlement_value;
        let next_ledger = PositionLedger {
            quantity: ZERO,
            cost_basis: ZERO,
            cash_flow: next_cash_flow,
            realized_pnl: next_cash_flow,
        };
        self.positions
            .insert(canonical.clone(), next_ledger.clone());
        self.cash += settlement_value;
        Ok(self.position_snapshot(&canonical, &next_ledger))
    }

    /// Apply one validated explicit marker.
    pub fn apply_marker(&mut self, marker: PositionMarker) -> PositionMarker {
        self.markers.insert(marker.symbol.clone(), marker.clone());
        marker
    }

    /// Create and apply an explicit price marker.
    pub fn mark(&mut self, symbol: &str, price: Decimal18) -> LiveResult<PositionMarker> {
        let marker = PositionMarker::new(canonical_symbol(symbol)?, price)?;
        Ok(self.apply_marker(marker))
    }

    /// The current explicit marker for a symbol, if any.
    pub fn marker(&self, symbol: &str) -> LiveResult<Option<PositionMarker>> {
        Ok(self.markers.get(&canonical_symbol(symbol)?).cloned())
    }

    /// The current immutable state for one previously held symbol.
    pub fn position(&self, symbol: &str) -> LiveResult<PositionSnapshot> {
        let canonical = canonical_symbol(symbol)?;
        let ledger = self
            .positions
            .get(&canonical)
            .ok_or_else(|| LiveError::Portfolio(format!("unknown position {symbol:?}")))?;
        Ok(self.position_snapshot(&canonical, ledger))
    }

    fn position_snapshot(&self, symbol: &Symbol, ledger: &PositionLedger) -> PositionSnapshot {
        PositionSnapshot {
            symbol: symbol.clone(),
            quantity: ledger.quantity,
            cost_basis: ledger.cost_basis,
            cash_flow: ledger.cash_flow,
            realized_pnl: ledger.realized_pnl,
            mark_price: self.markers.get(symbol).map(|marker| marker.price),
        }
    }
}
