//! Exact FIFO closed-trade accounting shared by execution views and result publication.
//! This pure ledger consumes booked fills, allocates both fees by matched quantity,
//! and retains open lots. It has no orders, market access or strategy policy.
use num::{Decimal18, DecimalError};
use std::collections::{BTreeMap, VecDeque};
use std::sync::Arc;

/// One closed-trade slice: a FIFO lot slice matched against one closing fill.
///
/// Fees follow `fill_quantity_share.v1`: the entry fee is the slice's share of
/// the opening lot's remaining fee, and the exit fee is its share of the closing
/// fill's remaining fee, each rounded half-even at scale 18.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClosedTrade {
    /// Trade identifier in exit order, starting at 1.
    pub trade_id: u64,
    /// Instrument symbol.
    pub symbol: String,
    /// `long` when the opening fill bought, `short` when it sold.
    pub side: String,
    /// Matched quantity, positive.
    pub quantity: Decimal18,
    /// Sequence of the fill that opened the matched lot.
    pub entry_sequence: u64,
    /// Sequence of the fill that closed the slice.
    pub exit_sequence: u64,
    /// Epoch nanoseconds of the opening fill.
    pub entry_timestamp_ns: i64,
    /// Epoch nanoseconds of the closing fill.
    pub exit_timestamp_ns: i64,
    /// Entry price.
    pub entry_price: Decimal18,
    /// Exit price.
    pub exit_price: Decimal18,
    /// Price PnL before fees.
    pub gross_pnl: Decimal18,
    /// Allocated entry fee.
    pub entry_fee: Decimal18,
    /// Allocated exit fee.
    pub exit_fee: Decimal18,
    /// `entry_fee + exit_fee`.
    pub fees: Decimal18,
    /// `gross_pnl - fees`.
    pub net_pnl: Decimal18,
}

impl ClosedTrade {
    /// Basis points of the matched entry notional; undefined if it rounds to zero.
    pub fn return_bps(&self, net: bool) -> Result<Option<Decimal18>, DecimalError> {
        let entry = self.quantity.checked_mul(self.entry_price)?;
        if entry.is_zero() {
            return Ok(None);
        }
        let pnl = if net { self.net_pnl } else { self.gross_pnl };
        pnl.checked_mul_div(Decimal18::from_i128(10_000)?, entry)
            .map(Some)
    }
}
#[derive(Clone, Debug)]
pub struct TradeFill {
    pub fill_id: String,
    pub symbol: String,
    pub buy: bool,
    pub quantity: Decimal18,
    pub price: Decimal18,
    pub fee: Decimal18,
    pub timestamp_ns: i64,
    pub sequence: u64,
}
#[derive(Clone, Debug)]
struct OpenLot {
    long: bool,
    sequence: u64,
    timestamp_ns: i64,
    price: Decimal18,
    quantity: Decimal18,
    fee: Decimal18,
}
#[derive(Clone, Debug, Default)]
pub struct TradeLedger {
    lots: BTreeMap<String, VecDeque<OpenLot>>,
    pub rows: Arc<Vec<ClosedTrade>>,
}
impl TradeLedger {
    /// Call in authoritative fill order. Arithmetic is checked before commit.
    pub fn apply(&mut self, fill: &TradeFill) -> Result<(), DecimalError> {
        if fill.quantity <= Decimal18::ZERO || fill.price <= Decimal18::ZERO {
            return Err(DecimalError::InvalidOperation);
        }
        let mut queue = self.lots.get(&fill.symbol).cloned().unwrap_or_default();
        let mut rows = Vec::new();
        let mut remaining = fill.quantity;
        let mut remaining_fee = fill.fee;
        while remaining.is_positive() {
            let Some(head) = queue.front() else {
                break;
            };
            if head.long == fill.buy {
                break;
            }
            let matched = head.quantity.min(remaining);
            let entry_fee = share(head.fee, matched, head.quantity)?;
            let exit_fee = share(remaining_fee, matched, remaining)?;
            let difference = if head.long {
                fill.price.checked_sub(head.price)?
            } else {
                head.price.checked_sub(fill.price)?
            };
            let gross_pnl = difference.checked_mul(matched)?;
            let fees = entry_fee.checked_add(exit_fee)?;
            rows.push(ClosedTrade {
                trade_id: (self.rows.len() + rows.len() + 1) as u64,
                symbol: fill.symbol.clone(),
                side: if head.long { "long" } else { "short" }.into(),
                quantity: matched,
                entry_sequence: head.sequence,
                exit_sequence: fill.sequence,
                entry_timestamp_ns: head.timestamp_ns,
                exit_timestamp_ns: fill.timestamp_ns,
                entry_price: head.price,
                exit_price: fill.price,
                gross_pnl,
                entry_fee,
                exit_fee,
                fees,
                net_pnl: gross_pnl.checked_sub(fees)?,
            });
            let head = queue.front_mut().expect("matched FIFO head");
            head.quantity = head.quantity.checked_sub(matched)?;
            head.fee = head.fee.checked_sub(entry_fee)?;
            if head.quantity.is_zero() {
                queue.pop_front();
            }
            remaining = remaining.checked_sub(matched)?;
            remaining_fee = remaining_fee.checked_sub(exit_fee)?;
        }
        if remaining.is_positive() {
            queue.push_back(OpenLot {
                long: fill.buy,
                sequence: fill.sequence,
                timestamp_ns: fill.timestamp_ns,
                price: fill.price,
                quantity: remaining,
                fee: remaining_fee,
            });
        }
        for row in &rows {
            row.return_bps(false)?;
            row.return_bps(true)?;
        }
        self.lots.insert(fill.symbol.clone(), queue);
        if !rows.is_empty() {
            Arc::make_mut(&mut self.rows).extend(rows);
        }
        Ok(())
    }
    pub fn open_fees(&self) -> Result<Decimal18, DecimalError> {
        self.lots
            .values()
            .flatten()
            .try_fold(Decimal18::ZERO, |total, lot| total.checked_add(lot.fee))
    }
}
fn share(amount: Decimal18, part: Decimal18, whole: Decimal18) -> Result<Decimal18, DecimalError> {
    if part == whole {
        Ok(amount)
    } else {
        amount.checked_mul_div(part, whole)
    }
}
