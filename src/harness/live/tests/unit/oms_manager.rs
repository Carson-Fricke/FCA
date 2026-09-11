use num::Decimal18;

use super::{ReplaceStatus, SessionAction, SingleLegOms};
use crate::accounting::ExecutionReport;
use crate::orders::{ExecutionType, OrderStatus, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn order(order_id: &str, tif: TimeInForce) -> SingleLegOrder {
    SingleLegOrder::new(
        order_id,
        Symbol::new("AAPL").unwrap(),
        Side::Buy,
        dec("10"),
        OrderType::Market,
        tif,
        None,
        None,
        None,
    )
    .unwrap()
}

fn report(
    oms: &SingleLegOms,
    order_id: &str,
    status: OrderStatus,
    execution_type: ExecutionType,
    report_id: &str,
    last_quantity: Decimal18,
    last_price: Option<Decimal18>,
) -> ExecutionReport {
    let snapshot = oms.state(order_id).unwrap();
    let filled = snapshot.filled_quantity + last_quantity;
    let mut notional = snapshot.filled_notional;
    if let Some(price) = last_price {
        notional += last_quantity * price;
    }
    let average = if filled == Decimal18::ZERO {
        None
    } else {
        Some(notional / filled)
    };
    ExecutionReport::new(
        report_id,
        order_id,
        status,
        execution_type,
        snapshot.order.quantity,
        filled,
        snapshot.order.quantity - filled,
        average,
        last_quantity,
        last_price,
        None,
    )
    .unwrap()
}

fn ack(oms: &mut SingleLegOms, order_id: &str) {
    let report = report(
        oms,
        order_id,
        OrderStatus::Accepted,
        ExecutionType::Acknowledged,
        "ack",
        Decimal18::ZERO,
        None,
    );
    oms.apply(report).unwrap();
}

#[test]
fn duplicate_submit_is_rejected() {
    let mut oms = SingleLegOms::new();
    oms.submit(order("o1", TimeInForce::Day)).unwrap();
    assert!(oms.submit(order("o1", TimeInForce::Day)).is_err());
}

#[test]
fn session_directives_expire_day_and_carry_gtc() {
    let mut oms = SingleLegOms::new();
    oms.submit(order("day", TimeInForce::Day)).unwrap();
    oms.submit(order("gtc", TimeInForce::Gtc)).unwrap();
    oms.submit(order("ioc", TimeInForce::Ioc)).unwrap();
    ack(&mut oms, "day");
    ack(&mut oms, "gtc");
    ack(&mut oms, "ioc");

    let directives = oms.session_directives();
    assert_eq!(directives.len(), 3);
    let actions: Vec<_> = directives
        .iter()
        .map(|directive| (directive.order_id.as_str(), directive.action))
        .collect();
    assert!(actions.contains(&("day", SessionAction::Expire)));
    assert!(actions.contains(&("gtc", SessionAction::Carry)));
    assert!(actions.contains(&("ioc", SessionAction::Expire)));

    let ioc = oms.ioc_expiry_directives();
    assert_eq!(ioc.len(), 1);
    assert_eq!(ioc[0].order_id, "ioc");
}

#[test]
fn replacement_requires_pending_record_and_routes() {
    let mut oms = SingleLegOms::new();
    oms.submit(order("orig", TimeInForce::Day)).unwrap();
    ack(&mut oms, "orig");
    let record = oms
        .request_replace("orig", order("repl", TimeInForce::Day))
        .unwrap();
    assert_eq!(record.status, ReplaceStatus::Pending);
    assert!(oms
        .request_replace("orig", order("repl2", TimeInForce::Day))
        .is_err());

    let replaced = report(
        &oms,
        "orig",
        OrderStatus::Replaced,
        ExecutionType::Replaced,
        "replaced",
        Decimal18::ZERO,
        None,
    );
    oms.apply(replaced).unwrap();
    assert_eq!(oms.replaced_by("orig"), Some("repl"));
    assert_eq!(oms.replaces("repl"), Some("orig"));
    assert_eq!(
        oms.replacement_chain("repl").unwrap(),
        vec!["orig".to_string(), "repl".to_string()]
    );
    assert_eq!(
        oms.replacement("orig").unwrap().status,
        ReplaceStatus::Applied
    );
}

#[test]
fn unknown_order_is_reported() {
    let oms = SingleLegOms::new();
    assert!(oms.state("missing").is_err());
}
