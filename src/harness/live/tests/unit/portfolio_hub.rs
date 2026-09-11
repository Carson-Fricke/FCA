use num::Decimal18;

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
