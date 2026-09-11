use num::Decimal18;

use super::{ExecutionReport, Fill};
use crate::orders::{ExecutionType, OrderStatus, Side};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

#[test]
fn execution_report_validates_quantity_identity() {
    let report = ExecutionReport::new(
        "r1",
        "o1",
        OrderStatus::Accepted,
        ExecutionType::Acknowledged,
        dec("10"),
        dec("0"),
        dec("10"),
        None,
        dec("0"),
        None,
        None,
    );
    assert!(report.is_ok());
    let bad = ExecutionReport::new(
        "r1",
        "o1",
        OrderStatus::Accepted,
        ExecutionType::Acknowledged,
        dec("10"),
        dec("0"),
        dec("9"),
        None,
        dec("0"),
        None,
        None,
    );
    assert!(bad.is_err());
}

#[test]
fn fill_allows_negative_rebate_fee() {
    let fill = Fill::new(
        "f1",
        "o1",
        Symbol::new("AAPL").unwrap(),
        Side::Buy,
        dec("1"),
        dec("100"),
        dec("-0.01"),
        Some("maker".into()),
    );
    assert!(fill.is_ok());
}
