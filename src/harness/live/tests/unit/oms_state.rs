use num::Decimal18;

use super::{OrderStateMachine, ZERO};
use crate::accounting::ExecutionReport;
use crate::error::LiveError;
use crate::orders::{ExecutionType, OrderStatus, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn order(order_id: &str, order_type: OrderType) -> SingleLegOrder {
    let (limit, stop) = match order_type {
        OrderType::Market => (None, None),
        OrderType::Limit => (Some(dec("101.25")), None),
        OrderType::Stop => (None, Some(dec("100.75"))),
        OrderType::StopLimit => (Some(dec("101.25")), Some(dec("100.75"))),
    };
    SingleLegOrder::new(
        order_id,
        Symbol::new("AAPL").unwrap(),
        Side::Buy,
        dec("10"),
        order_type,
        TimeInForce::Day,
        limit,
        stop,
        None,
    )
    .unwrap()
}

fn report(
    machine: &OrderStateMachine,
    status: OrderStatus,
    execution_type: ExecutionType,
    report_id: &str,
    last_quantity: Decimal18,
    last_price: Option<Decimal18>,
    average: Option<Decimal18>,
) -> ExecutionReport {
    let snapshot = machine.snapshot();
    let filled = snapshot.filled_quantity + last_quantity;
    let mut notional = snapshot.filled_notional;
    if let Some(price) = last_price {
        notional += last_quantity * price;
    }
    let calculated_average = if filled == ZERO {
        None
    } else {
        Some(notional / filled)
    };
    let average_fill_price = average.or(calculated_average);
    ExecutionReport::new(
        report_id,
        &snapshot.order.order_id,
        status,
        execution_type,
        snapshot.order.quantity,
        filled,
        snapshot.order.quantity - filled,
        average_fill_price,
        last_quantity,
        last_price,
        None,
    )
    .unwrap()
}

fn apply_ack(machine: &mut OrderStateMachine, report_id: &str) {
    let report = report(
        machine,
        OrderStatus::Accepted,
        ExecutionType::Acknowledged,
        report_id,
        ZERO,
        None,
        None,
    );
    machine.apply(report).unwrap();
}

#[test]
fn partial_fills_and_cancel_preserve_exact_decimal_ledgers() {
    let mut machine = OrderStateMachine::new(order("o1", OrderType::Market));
    apply_ack(&mut machine, "ack");
    machine
        .apply(report(
            &machine,
            OrderStatus::PartiallyFilled,
            ExecutionType::PartialFill,
            "partial-1",
            dec("0.125"),
            Some(dec("10.01")),
            None,
        ))
        .unwrap();
    let partial = machine
        .apply(report(
            &machine,
            OrderStatus::PartiallyFilled,
            ExecutionType::PartialFill,
            "partial-2",
            dec("0.375"),
            Some(dec("10.03")),
            None,
        ))
        .unwrap();
    machine
        .apply(report(
            &machine,
            OrderStatus::CancelPending,
            ExecutionType::Status,
            "cancel-pending",
            ZERO,
            None,
            None,
        ))
        .unwrap();
    let canceled = machine
        .apply(report(
            &machine,
            OrderStatus::Canceled,
            ExecutionType::Canceled,
            "canceled",
            ZERO,
            None,
            None,
        ))
        .unwrap();

    assert_eq!(partial.filled_quantity, dec("0.500"));
    assert_eq!(partial.remaining_quantity, dec("9.500"));
    assert_eq!(partial.filled_notional, dec("5.0125"));
    assert_eq!(partial.average_fill_price, Some(dec("10.025")));
    assert_eq!(canceled.filled_quantity, partial.filled_quantity);
    assert_eq!(canceled.filled_notional, partial.filled_notional);
}

#[test]
fn all_order_types_share_the_lifecycle() {
    let mut machine = OrderStateMachine::new(order("all", OrderType::StopLimit));
    apply_ack(&mut machine, "ack");
    let partial = machine
        .apply(report(
            &machine,
            OrderStatus::PartiallyFilled,
            ExecutionType::PartialFill,
            "partial",
            dec("3"),
            Some(dec("100")),
            None,
        ))
        .unwrap();
    let filled = machine
        .apply(report(
            &machine,
            OrderStatus::Filled,
            ExecutionType::Fill,
            "fill",
            dec("7"),
            Some(dec("101")),
            None,
        ))
        .unwrap();
    assert_eq!(partial.status, OrderStatus::PartiallyFilled);
    assert_eq!(filled.status, OrderStatus::Filled);
    assert_eq!(filled.filled_quantity, dec("10"));
    assert_eq!(filled.average_fill_price, Some(dec("100.7")));
}

#[test]
fn invalid_report_is_atomic_and_duplicate_is_rejected() {
    let mut machine = OrderStateMachine::new(order("o1", OrderType::Market));
    apply_ack(&mut machine, "ack");
    let before = machine.snapshot().clone();
    let bad_average = report(
        &machine,
        OrderStatus::PartiallyFilled,
        ExecutionType::PartialFill,
        "bad-average",
        dec("1"),
        Some(dec("100")),
        Some(dec("99.99")),
    );
    assert!(matches!(
        machine.apply(bad_average),
        Err(LiveError::ExecutionLedger(_))
    ));
    assert_eq!(machine.snapshot(), &before);
    assert_eq!(machine.reports().len(), 1);

    let duplicate = report(
        &machine,
        OrderStatus::Accepted,
        ExecutionType::Status,
        "ack",
        ZERO,
        None,
        None,
    );
    assert!(matches!(
        machine.apply(duplicate),
        Err(LiveError::DuplicateExecutionReport(_))
    ));
}

#[test]
fn execution_type_cannot_masquerade_as_another_status() {
    let mut machine = OrderStateMachine::new(order("o1", OrderType::Market));
    let before = machine.snapshot().clone();
    let mismatched = report(
        &machine,
        OrderStatus::Filled,
        ExecutionType::PartialFill,
        "mismatch",
        ZERO,
        None,
        None,
    );
    assert!(matches!(
        machine.apply(mismatched),
        Err(LiveError::IllegalTransition(_))
    ));
    assert_eq!(machine.snapshot(), &before);
}

#[test]
fn terminal_states_reject_further_reports() {
    let mut machine = OrderStateMachine::new(order("o1", OrderType::Market));
    apply_ack(&mut machine, "ack");
    machine
        .apply(report(
            &machine,
            OrderStatus::Canceled,
            ExecutionType::Canceled,
            "cancel",
            ZERO,
            None,
            None,
        ))
        .unwrap();
    let after = machine.apply(report(
        &machine,
        OrderStatus::Canceled,
        ExecutionType::Status,
        "late",
        ZERO,
        None,
        None,
    ));
    assert!(matches!(after, Err(LiveError::IllegalTransition(_))));
}
