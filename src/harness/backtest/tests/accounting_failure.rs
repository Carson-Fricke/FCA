//! Replay accounting reports failure atomically at its book boundary.
use backtest::backtest::portfolio::{BookFill, VirtualBook};
use num::Decimal;

#[test]
fn overflow_does_not_commit_cash_positions_or_fill_identity() {
    let mut book = VirtualBook::new("strategy", Decimal::from_i128(1000));
    let before = book.snapshot();
    let fill = BookFill {
        fill_id: "fill".into(),
        symbol: "SPY".into(),
        side: "buy".into(),
        quantity: "9E+2147483647".parse().unwrap(),
        price: "1E+2147483647".parse().unwrap(),
        fee: Decimal::ZERO,
    };
    assert!(book.apply_fill(&fill).is_err());
    assert_eq!(book.snapshot(), before);
    assert_eq!(book.fill_count(), 0);
}
