//! Failed accounting must leave the public book unchanged, including fill identity.
use harness_trading::{
    accounting::Fill, orders::Side, portfolio::book::VirtualBook, symbol::Symbol,
};
use num::Decimal18;

#[test]
fn overflow_does_not_commit_cash_positions_or_fill_identity() {
    let mut book = VirtualBook::new("strategy", Decimal18::from_i64(1000)).unwrap();
    let before = book.snapshot();
    let fill = Fill::new(
        "fill",
        "order",
        Symbol::new("SPY").unwrap(),
        Side::Buy,
        "99999999999999999999".parse().unwrap(),
        Decimal18::from_i64(2),
        Decimal18::ZERO,
        None,
    )
    .unwrap();
    assert!(book.apply_fill(&fill).is_err());
    assert_eq!(book.snapshot(), before);
    assert!(book.fill_ids().is_empty());
    let corrected = Fill {
        quantity: Decimal18::ONE,
        ..fill
    };
    assert!(book.apply_fill(&corrected).is_ok());
    assert_eq!(book.fill_count(), 1);
    assert_eq!(book.cash(), Decimal18::from_i64(998));
}
