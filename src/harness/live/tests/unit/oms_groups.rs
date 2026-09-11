use num::Decimal18;

use super::{GroupOrderType, GroupStatus, MultiLegOms, MultiLegOrderGroup};
use crate::accounting::Fill;
use crate::orders::{MultiLegOrder, OrderLeg, OrderType, Side};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn leg(symbol: &str, side: Side, quantity: &str) -> OrderLeg {
    OrderLeg::new(
        Symbol::new(symbol).unwrap(),
        side,
        dec(quantity),
        dec("1"),
        None,
    )
    .unwrap()
}

fn call_spread(order_id: &str, order_type: OrderType, limit: Option<&str>) -> MultiLegOrder {
    MultiLegOrder::new(
        order_id,
        vec![leg("AAPL", Side::Buy, "1"), leg("MSFT", Side::Sell, "1")],
        order_type,
        crate::orders::TimeInForce::Day,
        None,
        limit.map(dec),
    )
    .unwrap()
}

fn fill(
    order_id: &str,
    fill_id: &str,
    symbol: &str,
    side: Side,
    quantity: &str,
    price: &str,
) -> Fill {
    Fill::new(
        fill_id,
        order_id,
        Symbol::new(symbol).unwrap(),
        side,
        dec(quantity),
        dec(price),
        Decimal18::ZERO,
        Some("taker".into()),
    )
    .unwrap()
}

#[test]
fn group_fill_is_atomic_and_net_price_is_ratio_weighted() {
    let order = call_spread("g1", OrderType::Market, None);
    let mut group = MultiLegOrderGroup::new(order, None).unwrap();
    group.accept().unwrap();
    group.begin_evaluation().unwrap();
    let snapshot = group
        .fill(vec![
            fill("g1", "f1", "AAPL", Side::Buy, "1", "100"),
            fill("g1", "f2", "MSFT", Side::Sell, "1", "101"),
        ])
        .unwrap();
    assert_eq!(snapshot.status, GroupStatus::Filled);
    assert_eq!(snapshot.net_price, Some(dec("-1")));
    assert_eq!(snapshot.net_credit(), dec("1"));
    assert_eq!(snapshot.net_debit(), Decimal18::ZERO);
    assert_eq!(snapshot.gross_cash, dec("1"));
}

#[test]
fn partial_group_fill_is_rejected() {
    let order = call_spread("g2", OrderType::Market, None);
    let mut group = MultiLegOrderGroup::new(order, None).unwrap();
    group.accept().unwrap();
    group.begin_evaluation().unwrap();
    let result = group.fill(vec![fill("g2", "f1", "AAPL", Side::Buy, "1", "100")]);
    assert!(result.is_err());
    assert_eq!(group.snapshot().status, GroupStatus::Evaluating);
}

#[test]
fn net_debit_limit_is_enforced() {
    let order = call_spread("g3", OrderType::Limit, Some("1"));
    let mut group = MultiLegOrderGroup::new(order, Some(GroupOrderType::NetDebit)).unwrap();
    group.accept().unwrap();
    group.begin_evaluation().unwrap();
    let result = group.fill(vec![
        fill("g3", "f1", "AAPL", Side::Buy, "1", "100"),
        fill("g3", "f2", "MSFT", Side::Sell, "1", "101"),
    ]);
    assert!(result.is_err());
}

#[test]
fn oms_rejects_duplicate_fill_ids() {
    let mut oms = MultiLegOms::new();
    oms.submit(call_spread("g4", OrderType::Market, None), None)
        .unwrap();
    oms.accept("g4").unwrap();
    oms.begin_evaluation("g4").unwrap();
    oms.fill(
        "g4",
        vec![
            fill("g4", "f1", "AAPL", Side::Buy, "1", "100"),
            fill("g4", "f2", "MSFT", Side::Sell, "1", "101"),
        ],
    )
    .unwrap();
    oms.submit(call_spread("g5", OrderType::Market, None), None)
        .unwrap();
    oms.accept("g5").unwrap();
    oms.begin_evaluation("g5").unwrap();
    let result = oms.fill(
        "g5",
        vec![
            fill("g5", "f1", "AAPL", Side::Buy, "1", "100"),
            fill("g5", "f3", "MSFT", Side::Sell, "1", "101"),
        ],
    );
    assert!(result.is_err());
}

#[test]
fn unknown_group_is_reported() {
    let oms = MultiLegOms::new();
    assert!(oms.state("missing").is_err());
}
