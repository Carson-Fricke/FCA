use num::Decimal18;

use super::VirtualBook;
use crate::accounting::Fill;
use crate::orders::Side;
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn fill(fill_id: &str, side: Side, quantity: &str, price: &str, fee: &str) -> Fill {
    Fill::new(
        fill_id,
        "o1",
        Symbol::new("AAPL").unwrap(),
        side,
        dec(quantity),
        dec(price),
        dec(fee),
        None,
    )
    .unwrap()
}

#[test]
fn round_trip_realizes_exact_pnl_and_fees() {
    let mut book = VirtualBook::new("b1", dec("1000")).unwrap();
    book.apply_fill(&fill("f1", Side::Buy, "10", "100", "1"))
        .unwrap();
    let closed = book
        .apply_fill(&fill("f2", Side::Sell, "10", "110", "1"))
        .unwrap();
    assert!(closed.is_flat());
    assert_eq!(closed.realized_pnl, dec("98"));
    assert_eq!(book.cash(), dec("1098"));
    assert_eq!(book.realized_pnl(), dec("98"));
}

#[test]
fn partial_close_releases_proportional_basis() {
    let mut book = VirtualBook::new("b1", dec("1000")).unwrap();
    book.apply_fill(&fill("f1", Side::Buy, "10", "100", "0"))
        .unwrap();
    let partial = book
        .apply_fill(&fill("f2", Side::Sell, "4", "110", "0"))
        .unwrap();
    assert_eq!(partial.quantity, dec("6"));
    assert_eq!(partial.cost_basis, dec("600"));
    assert_eq!(partial.realized_pnl, dec("40"));
}

#[test]
fn duplicate_fill_is_rejected() {
    let mut book = VirtualBook::new("b1", dec("1000")).unwrap();
    book.apply_fill(&fill("f1", Side::Buy, "1", "100", "0"))
        .unwrap();
    assert!(book
        .apply_fill(&fill("f1", Side::Buy, "1", "100", "0"))
        .is_err());
}

#[test]
fn mark_revalues_open_positions() {
    let mut book = VirtualBook::new("b1", dec("1000")).unwrap();
    book.apply_fill(&fill("f1", Side::Buy, "10", "100", "0"))
        .unwrap();
    assert!(book.equity().is_none());
    book.mark("AAPL", dec("105")).unwrap();
    let snapshot = book.snapshot();
    assert_eq!(snapshot.equity(), Some(dec("1050")));
    assert_eq!(snapshot.unrealized_pnl(), Some(dec("50")));
}
