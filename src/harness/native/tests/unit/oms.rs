use super::{
    computed_average, report_from, snapshots_equal, AverageArg, Error, ExecutionType,
    OrderSnapshot, OrderStateMachine, OrderStatus, ReplaceStatus, SessionAction, SingleLegOMS,
    SingleLegOrder,
};
use crate::decimal::from_int;
use crate::market::{OrderType, Side, TimeInForce};

fn ten() -> i128 {
    from_int(10)
}

fn test_order(order_id: &str) -> SingleLegOrder {
    SingleLegOrder {
        order_id: order_id.to_owned(),
        symbol: "AAPL".to_owned(),
        side: Side::Buy,
        quantity: ten(),
        order_type: OrderType::Limit,
        time_in_force: TimeInForce::Day,
    }
}

fn manager_order(
    order_id: &str,
    time_in_force: TimeInForce,
    side: Side,
    symbol: &str,
    quantity: i128,
) -> SingleLegOrder {
    SingleLegOrder {
        order_id: order_id.to_owned(),
        symbol: symbol.to_owned(),
        side,
        quantity,
        order_type: OrderType::Limit,
        time_in_force,
    }
}

#[allow(clippy::too_many_arguments)]
fn report_for(
    machine: &OrderStateMachine,
    status: OrderStatus,
    execution_type: ExecutionType,
    report_id: &str,
    last_quantity: i128,
    last_price: Option<i128>,
    average: AverageArg,
) -> Result<super::ExecutionReport, Error> {
    report_from(
        &machine.snapshot,
        status,
        execution_type,
        report_id,
        last_quantity,
        last_price,
        average,
    )
}

fn apply_ack(machine: &mut OrderStateMachine, report_id: &str) -> Result<(), Error> {
    machine.apply(report_for(
        machine,
        OrderStatus::Accepted,
        ExecutionType::Acknowledged,
        report_id,
        0,
        None,
        computed_average(),
    )?)?;
    Ok(())
}

fn apply_partial(machine: &mut OrderStateMachine, report_id: &str) -> Result<(), Error> {
    machine.apply(report_for(
        machine,
        OrderStatus::PartiallyFilled,
        ExecutionType::PartialFill,
        report_id,
        from_int(2),
        Some(from_int(100) + 250_000_000_000_000_000),
        computed_average(),
    )?)?;
    Ok(())
}

fn machine_in_status(status: OrderStatus) -> Result<OrderStateMachine, Error> {
    let mut machine = OrderStateMachine::new(test_order("source"));
    match status {
        OrderStatus::New => {}
        OrderStatus::Rejected => {
            machine.apply(report_for(
                &machine,
                OrderStatus::Rejected,
                ExecutionType::Rejected,
                "setup-rejected",
                0,
                None,
                computed_average(),
            )?)?;
        }
        OrderStatus::Accepted => apply_ack(&mut machine, "setup-ack")?,
        OrderStatus::PartiallyFilled => {
            apply_ack(&mut machine, "setup-ack")?;
            apply_partial(&mut machine, "setup-partial")?;
        }
        OrderStatus::CancelPending => {
            apply_ack(&mut machine, "setup-ack")?;
            machine.apply(report_for(
                &machine,
                OrderStatus::CancelPending,
                ExecutionType::Status,
                "setup-cancel-pending",
                0,
                None,
                computed_average(),
            )?)?;
        }
        OrderStatus::Filled => {
            apply_ack(&mut machine, "setup-ack")?;
            machine.apply(report_for(
                &machine,
                OrderStatus::Filled,
                ExecutionType::Fill,
                "setup-filled",
                ten(),
                Some(from_int(100)),
                computed_average(),
            )?)?;
        }
        OrderStatus::Canceled => {
            apply_ack(&mut machine, "setup-ack")?;
            machine.apply(report_for(
                &machine,
                OrderStatus::Canceled,
                ExecutionType::Canceled,
                "setup-canceled",
                0,
                None,
                computed_average(),
            )?)?;
        }
        OrderStatus::Expired => {
            apply_ack(&mut machine, "setup-ack")?;
            machine.apply(report_for(
                &machine,
                OrderStatus::Expired,
                ExecutionType::Expired,
                "setup-expired",
                0,
                None,
                computed_average(),
            )?)?;
        }
        OrderStatus::Replaced => {
            apply_ack(&mut machine, "setup-ack")?;
            machine.apply(report_for(
                &machine,
                OrderStatus::Replaced,
                ExecutionType::Replaced,
                "setup-replaced",
                0,
                None,
                computed_average(),
            )?)?;
        }
    }
    Ok(machine)
}

fn candidate_report(
    target: OrderStatus,
    machine: &OrderStateMachine,
) -> Result<super::ExecutionReport, Error> {
    let execution_type = match target {
        OrderStatus::New => ExecutionType::Status,
        OrderStatus::Accepted => {
            if machine.snapshot.status == OrderStatus::New {
                ExecutionType::Acknowledged
            } else {
                ExecutionType::Status
            }
        }
        OrderStatus::PartiallyFilled => ExecutionType::PartialFill,
        OrderStatus::Filled => ExecutionType::Fill,
        OrderStatus::CancelPending => ExecutionType::Status,
        OrderStatus::Canceled => ExecutionType::Canceled,
        OrderStatus::Rejected => ExecutionType::Rejected,
        OrderStatus::Expired => ExecutionType::Expired,
        OrderStatus::Replaced => ExecutionType::Replaced,
    };
    let mut last_quantity: i128 = 0;
    let mut last_price: Option<i128> = None;
    match target {
        OrderStatus::PartiallyFilled => {
            if machine.snapshot.remaining_quantity > 0 {
                last_quantity = from_int(1).min(machine.snapshot.remaining_quantity);
                last_price = Some(from_int(100));
            }
        }
        OrderStatus::Filled => {
            last_quantity = machine.snapshot.remaining_quantity;
            if last_quantity > 0 {
                last_price = Some(from_int(100));
            }
        }
        _ => {}
    }
    report_for(
        machine,
        target,
        execution_type,
        "candidate",
        last_quantity,
        last_price,
        computed_average(),
    )
}

fn legal_targets(source: OrderStatus) -> &'static [OrderStatus] {
    match source {
        OrderStatus::New => &[
            OrderStatus::New,
            OrderStatus::Accepted,
            OrderStatus::Rejected,
        ],
        OrderStatus::Accepted => &[
            OrderStatus::Accepted,
            OrderStatus::PartiallyFilled,
            OrderStatus::Filled,
            OrderStatus::CancelPending,
            OrderStatus::Canceled,
            OrderStatus::Expired,
            OrderStatus::Replaced,
        ],
        OrderStatus::PartiallyFilled => &[
            OrderStatus::PartiallyFilled,
            OrderStatus::Filled,
            OrderStatus::CancelPending,
            OrderStatus::Canceled,
            OrderStatus::Expired,
            OrderStatus::Replaced,
        ],
        OrderStatus::CancelPending => &[
            OrderStatus::Accepted,
            OrderStatus::PartiallyFilled,
            OrderStatus::Filled,
            OrderStatus::CancelPending,
            OrderStatus::Canceled,
            OrderStatus::Expired,
        ],
        OrderStatus::Filled
        | OrderStatus::Canceled
        | OrderStatus::Rejected
        | OrderStatus::Expired
        | OrderStatus::Replaced => &[],
    }
}

const ALL_STATUSES: [OrderStatus; 9] = [
    OrderStatus::New,
    OrderStatus::Accepted,
    OrderStatus::PartiallyFilled,
    OrderStatus::Filled,
    OrderStatus::CancelPending,
    OrderStatus::Canceled,
    OrderStatus::Rejected,
    OrderStatus::Expired,
    OrderStatus::Replaced,
];

#[test]
fn all_order_types_and_time_in_force_values_share_the_lifecycle() {
    let order_types = [
        OrderType::Market,
        OrderType::Limit,
        OrderType::Stop,
        OrderType::StopLimit,
    ];
    let tifs = [TimeInForce::Day, TimeInForce::Gtc, TimeInForce::Ioc];
    for order_type in order_types {
        for time_in_force in tifs {
            let order = SingleLegOrder {
                order_id: format!("{order_type:?}-{time_in_force:?}"),
                symbol: "AAPL".to_owned(),
                side: Side::Buy,
                quantity: ten(),
                order_type,
                time_in_force,
            };
            let mut machine = OrderStateMachine::new(order);
            apply_ack(&mut machine, "ack").unwrap();
            let partial = machine
                .apply(
                    report_for(
                        &machine,
                        OrderStatus::PartiallyFilled,
                        ExecutionType::PartialFill,
                        "partial",
                        from_int(3),
                        Some(from_int(100)),
                        computed_average(),
                    )
                    .unwrap(),
                )
                .unwrap();
            let filled = machine
                .apply(
                    report_for(
                        &machine,
                        OrderStatus::Filled,
                        ExecutionType::Fill,
                        "fill",
                        from_int(7),
                        Some(from_int(101)),
                        computed_average(),
                    )
                    .unwrap(),
                )
                .unwrap();
            assert_eq!(partial.status, OrderStatus::PartiallyFilled);
            assert_eq!(filled.status, OrderStatus::Filled);
            assert_eq!(filled.filled_quantity, ten());
            assert_eq!(filled.average_fill_price, Some(100_700_000_000_000_000_000));
            assert_eq!(machine.snapshot.order.order_type, order_type);
            assert_eq!(machine.snapshot.order.time_in_force, time_in_force);
        }
    }
}

#[test]
fn partial_fills_and_cancel_preserve_exact_decimal_ledgers() {
    let mut machine = OrderStateMachine::new(test_order("order-1"));
    apply_ack(&mut machine, "ack").unwrap();
    machine
        .apply(
            report_for(
                &machine,
                OrderStatus::PartiallyFilled,
                ExecutionType::PartialFill,
                "partial-1",
                125_000_000_000_000_000,
                Some(10_010_000_000_000_000_000),
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();
    let partial = machine
        .apply(
            report_for(
                &machine,
                OrderStatus::PartiallyFilled,
                ExecutionType::PartialFill,
                "partial-2",
                375_000_000_000_000_000,
                Some(10_030_000_000_000_000_000),
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();
    machine
        .apply(
            report_for(
                &machine,
                OrderStatus::CancelPending,
                ExecutionType::Status,
                "cancel-pending",
                0,
                None,
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();
    let canceled = machine
        .apply(
            report_for(
                &machine,
                OrderStatus::Canceled,
                ExecutionType::Canceled,
                "canceled",
                0,
                None,
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();

    assert_eq!(partial.filled_quantity, 500_000_000_000_000_000);
    assert_eq!(partial.remaining_quantity, 9_500_000_000_000_000_000);
    assert_eq!(partial.filled_notional, 5_012_500_000_000_000_000);
    assert_eq!(partial.average_fill_price, Some(10_025_000_000_000_000_000));
    assert_eq!(partial.filled_quantity, canceled.filled_quantity);
    assert_eq!(partial.remaining_quantity, canceled.remaining_quantity);
    assert_eq!(partial.filled_notional, canceled.filled_notional);
    assert_eq!(partial.average_fill_price, canceled.average_fill_price);
}

#[test]
fn invalid_report_is_atomic_and_duplicate_report_id_is_rejected() {
    let mut machine = OrderStateMachine::new(test_order("order-1"));
    apply_ack(&mut machine, "ack").unwrap();
    let before = machine.snapshot.clone();

    let bad_average = report_for(
        &machine,
        OrderStatus::PartiallyFilled,
        ExecutionType::PartialFill,
        "bad-average",
        crate::decimal::ONE,
        Some(from_int(100)),
        AverageArg::Explicit(Some(from_int(99) + 990_000_000_000_000_000)),
    )
    .unwrap();
    assert_eq!(machine.apply(bad_average), Err(Error::ExecutionLedger));
    assert!(snapshots_equal(&before, &machine.snapshot));
    assert_eq!(machine.reports().len(), 1);

    let duplicate = report_for(
        &machine,
        OrderStatus::Accepted,
        ExecutionType::Status,
        "ack",
        0,
        None,
        computed_average(),
    )
    .unwrap();
    assert_eq!(machine.apply(duplicate), Err(Error::DuplicateReport));
    assert!(snapshots_equal(&before, &machine.snapshot));
}

#[test]
fn execution_type_cannot_masquerade_as_another_status() {
    let cases = [
        (ExecutionType::Acknowledged, OrderStatus::Canceled),
        (ExecutionType::PartialFill, OrderStatus::Filled),
        (ExecutionType::Fill, OrderStatus::PartiallyFilled),
        (ExecutionType::Canceled, OrderStatus::Expired),
        (ExecutionType::Rejected, OrderStatus::Accepted),
        (ExecutionType::Expired, OrderStatus::Canceled),
        (ExecutionType::Replaced, OrderStatus::Canceled),
        (ExecutionType::Status, OrderStatus::Filled),
    ];
    for (execution_type, status) in cases {
        let mut machine = OrderStateMachine::new(test_order("order-1"));
        let before = machine.snapshot.clone();
        let mismatch = report_for(
            &machine,
            status,
            execution_type,
            "mismatch",
            0,
            None,
            computed_average(),
        )
        .unwrap();
        assert_eq!(machine.apply(mismatch), Err(Error::IllegalTransition));
        assert!(snapshots_equal(&before, &machine.snapshot));
    }
}

#[test]
fn transition_matrix_is_exhaustive() {
    for source in ALL_STATUSES {
        for target in ALL_STATUSES {
            let mut machine = machine_in_status(source).unwrap();
            let candidate = candidate_report(target, &machine).unwrap();
            if legal_targets(source).contains(&target) {
                let applied = machine.apply(candidate).unwrap();
                assert_eq!(applied.status, target);
            } else {
                let before = machine.snapshot.clone();
                assert_eq!(machine.apply(candidate), Err(Error::IllegalTransition));
                assert!(snapshots_equal(&before, &machine.snapshot));
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn oms_report(
    oms: &SingleLegOMS,
    order_id: &str,
    status: OrderStatus,
    execution_type: ExecutionType,
    report_id: &str,
    last_quantity: i128,
    last_price: Option<i128>,
    average: AverageArg,
) -> Result<super::ExecutionReport, Error> {
    report_from(
        &oms.state(order_id)?,
        status,
        execution_type,
        report_id,
        last_quantity,
        last_price,
        average,
    )
}

fn acknowledge(oms: &mut SingleLegOMS, order_id: &str, report_id: &str) -> Result<(), Error> {
    let report = oms_report(
        oms,
        order_id,
        OrderStatus::Accepted,
        ExecutionType::Acknowledged,
        report_id,
        0,
        None,
        computed_average(),
    )?;
    oms.apply(report)?;
    Ok(())
}

fn expire_report(
    oms: &SingleLegOMS,
    order_id: &str,
    report_id: &str,
) -> Result<super::ExecutionReport, Error> {
    oms_report(
        oms,
        order_id,
        OrderStatus::Expired,
        ExecutionType::Expired,
        report_id,
        0,
        None,
        computed_average(),
    )
}

#[test]
fn replace_is_reserved_then_confirmed_atomically_by_report() {
    let mut oms = SingleLegOMS::new();
    oms.submit(test_order("original")).unwrap();
    acknowledge(&mut oms, "original", "ack-original").unwrap();
    oms.apply(
        oms_report(
            &oms,
            "original",
            OrderStatus::PartiallyFilled,
            ExecutionType::PartialFill,
            "partial-original",
            1_250_000_000_000_000_000,
            Some(from_int(100) + 100_000_000_000_000_000),
            computed_average(),
        )
        .unwrap(),
    )
    .unwrap();
    let replacement = manager_order(
        "replacement",
        TimeInForce::Day,
        Side::Buy,
        "AAPL",
        from_int(6),
    );

    let pending = oms.request_replace("original", replacement).unwrap();
    assert_eq!(pending.status, ReplaceStatus::Pending);
    assert_eq!(
        oms.state("original").unwrap().status,
        OrderStatus::PartiallyFilled
    );
    assert!(oms.replaced_by("original").is_none());
    assert_eq!(oms.state("replacement"), Err(Error::UnknownOrder));

    let replaced = oms
        .apply(
            oms_report(
                &oms,
                "original",
                OrderStatus::Replaced,
                ExecutionType::Replaced,
                "replaced-original",
                0,
                None,
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();

    assert_eq!(replaced.status, OrderStatus::Replaced);
    assert_eq!(replaced.filled_quantity, 1_250_000_000_000_000_000);
    assert_eq!(
        replaced.average_fill_price,
        Some(100_100_000_000_000_000_000)
    );
    assert_eq!(oms.state("replacement").unwrap().status, OrderStatus::New);
    assert_eq!(oms.replaced_by("original"), Some("replacement"));
    assert_eq!(oms.replaces("replacement"), Some("original"));

    let chain = oms.replacement_chain("replacement").unwrap();
    assert_eq!(chain.len(), 2);
    assert_eq!(chain[0], "original");
    assert_eq!(chain[1], "replacement");

    let applied = oms.replacement("original").unwrap();
    assert_eq!(applied.status, ReplaceStatus::Applied);
    assert_eq!(applied.report_id.as_deref(), Some("replaced-original"));

    let active = oms.active_states();
    assert_eq!(active.len(), 1);
    assert_eq!(active[0].order_id(), "replacement");
}

#[test]
fn failed_replace_report_changes_nothing_and_keeps_reservation() {
    let mut oms = SingleLegOMS::new();
    oms.submit(test_order("original")).unwrap();
    acknowledge(&mut oms, "original", "ack-original").unwrap();
    oms.apply(
        oms_report(
            &oms,
            "original",
            OrderStatus::PartiallyFilled,
            ExecutionType::PartialFill,
            "partial",
            crate::decimal::ONE,
            Some(from_int(100)),
            computed_average(),
        )
        .unwrap(),
    )
    .unwrap();
    oms.request_replace("original", test_order("replacement"))
        .unwrap();
    let before = oms.state("original").unwrap();
    let invalid = oms_report(
        &oms,
        "original",
        OrderStatus::Replaced,
        ExecutionType::Replaced,
        "bad-replaced",
        0,
        None,
        AverageArg::Explicit(Some(from_int(99))),
    )
    .unwrap();

    assert_eq!(oms.apply(invalid), Err(Error::ExecutionLedger));
    assert!(snapshots_equal(&before, &oms.state("original").unwrap()));
    let pending = oms.replacement("original").unwrap();
    assert_eq!(pending.status, ReplaceStatus::Pending);
    assert_eq!(oms.state("replacement"), Err(Error::UnknownOrder));
}

#[test]
fn replace_requires_accepted_compatible_order_and_pending_request() {
    let mut oms = SingleLegOMS::new();
    oms.submit(test_order("original")).unwrap();

    assert_eq!(
        oms.request_replace("original", test_order("too-early")),
        Err(Error::ReplaceConflict)
    );

    acknowledge(&mut oms, "original", "ack-original").unwrap();
    assert_eq!(
        oms.request_replace(
            "original",
            manager_order("wrong-symbol", TimeInForce::Day, Side::Buy, "MSFT", ten())
        ),
        Err(Error::ReplaceConflict)
    );
    assert_eq!(
        oms.request_replace(
            "original",
            manager_order("wrong-side", TimeInForce::Day, Side::Sell, "AAPL", ten())
        ),
        Err(Error::ReplaceConflict)
    );

    let without_pending = oms_report(
        &oms,
        "original",
        OrderStatus::Replaced,
        ExecutionType::Replaced,
        "without-pending",
        0,
        None,
        computed_average(),
    )
    .unwrap();
    assert_eq!(oms.apply(without_pending), Err(Error::ReplaceConflict));
    assert_eq!(oms.state("original").unwrap().status, OrderStatus::Accepted);
}

#[test]
fn terminal_report_aborts_pending_replace_and_preserves_cancel_ledger() {
    let mut oms = SingleLegOMS::new();
    oms.submit(test_order("original")).unwrap();
    acknowledge(&mut oms, "original", "ack-original").unwrap();
    oms.request_replace("original", test_order("reserved"))
        .unwrap();

    oms.apply(
        oms_report(
            &oms,
            "original",
            OrderStatus::CancelPending,
            ExecutionType::Status,
            "cancel-pending",
            0,
            None,
            computed_average(),
        )
        .unwrap(),
    )
    .unwrap();
    let canceled = oms
        .apply(
            oms_report(
                &oms,
                "original",
                OrderStatus::Canceled,
                ExecutionType::Canceled,
                "canceled",
                0,
                None,
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();

    assert_eq!(canceled.filled_quantity, 0);
    assert_eq!(canceled.remaining_quantity, ten());
    let replacement = oms.replacement("original").unwrap();
    assert_eq!(replacement.status, ReplaceStatus::Aborted);
    assert_eq!(replacement.report_id.as_deref(), Some("canceled"));
    assert_eq!(oms.state("reserved"), Err(Error::UnknownOrder));
}

#[test]
fn ioc_expiry_and_gtc_session_carry_over() {
    let mut oms = SingleLegOMS::new();
    oms.submit(manager_order(
        "local-new",
        TimeInForce::Gtc,
        Side::Buy,
        "AAPL",
        ten(),
    ))
    .unwrap();
    oms.submit(manager_order(
        "day",
        TimeInForce::Day,
        Side::Buy,
        "AAPL",
        ten(),
    ))
    .unwrap();
    oms.submit(manager_order(
        "gtc",
        TimeInForce::Gtc,
        Side::Buy,
        "AAPL",
        ten(),
    ))
    .unwrap();
    oms.submit(manager_order(
        "ioc",
        TimeInForce::Ioc,
        Side::Buy,
        "AAPL",
        ten(),
    ))
    .unwrap();
    acknowledge(&mut oms, "day", "ack-day").unwrap();
    acknowledge(&mut oms, "gtc", "ack-gtc").unwrap();
    acknowledge(&mut oms, "ioc", "ack-ioc").unwrap();

    let directives = oms.session_directives();
    assert_eq!(directives.len(), 3);
    assert_eq!(directives[0].order_id, "day");
    assert_eq!(directives[0].action, SessionAction::Expire);
    assert_eq!(directives[1].order_id, "gtc");
    assert_eq!(directives[1].action, SessionAction::Carry);
    assert_eq!(directives[2].order_id, "ioc");
    assert_eq!(directives[2].action, SessionAction::Expire);

    let ioc_directives = oms.ioc_expiry_directives();
    assert_eq!(ioc_directives.len(), 1);
    assert_eq!(ioc_directives[0].order_id, "ioc");
    assert_eq!(ioc_directives[0].action, SessionAction::Expire);

    oms.apply(
        oms_report(
            &oms,
            "ioc",
            OrderStatus::PartiallyFilled,
            ExecutionType::PartialFill,
            "ioc-partial",
            2_500_000_000_000_000_000,
            Some(from_int(100) + 50_000_000_000_000_000),
            computed_average(),
        )
        .unwrap(),
    )
    .unwrap();
    let ioc_expired = oms
        .apply_ioc_expiry(expire_report(&oms, "ioc", "expired-ioc").unwrap())
        .unwrap();
    let day_expired = oms
        .apply_session_expiry(expire_report(&oms, "day", "expired-day").unwrap())
        .unwrap();

    assert_eq!(ioc_expired.status, OrderStatus::Expired);
    assert_eq!(ioc_expired.filled_quantity, 2_500_000_000_000_000_000);
    assert_eq!(ioc_expired.remaining_quantity, 7_500_000_000_000_000_000);
    assert_eq!(
        ioc_expired.average_fill_price,
        Some(100_050_000_000_000_000_000)
    );
    assert_eq!(day_expired.status, OrderStatus::Expired);
    assert_eq!(oms.state("gtc").unwrap().status, OrderStatus::Accepted);
    assert_eq!(oms.state("local-new").unwrap().status, OrderStatus::New);

    let later = oms.session_directives();
    assert_eq!(later.len(), 1);
    assert_eq!(later[0].order_id, "gtc");
    assert_eq!(later[0].action, SessionAction::Carry);

    let filled = oms
        .apply(
            oms_report(
                &oms,
                "gtc",
                OrderStatus::Filled,
                ExecutionType::Fill,
                "gtc-next-session-fill",
                ten(),
                Some(from_int(102) + 150_000_000_000_000_000),
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();
    assert_eq!(filled.status, OrderStatus::Filled);
    assert_eq!(filled.average_fill_price, Some(102_150_000_000_000_000_000));
}

#[test]
fn expiry_policy_refusals() {
    let mut oms = SingleLegOMS::new();
    oms.submit(manager_order(
        "gtc",
        TimeInForce::Gtc,
        Side::Buy,
        "AAPL",
        ten(),
    ))
    .unwrap();
    oms.submit(manager_order(
        "day",
        TimeInForce::Day,
        Side::Buy,
        "AAPL",
        ten(),
    ))
    .unwrap();
    acknowledge(&mut oms, "gtc", "ack-gtc").unwrap();
    acknowledge(&mut oms, "day", "ack-day").unwrap();

    assert_eq!(
        oms.apply_session_expiry(expire_report(&oms, "gtc", "expired-gtc").unwrap()),
        Err(Error::ExpiryPolicy)
    );
    assert_eq!(
        oms.apply_ioc_expiry(expire_report(&oms, "day", "expired-day").unwrap()),
        Err(Error::ExpiryPolicy)
    );
    let not_expiry = oms_report(
        &oms,
        "day",
        OrderStatus::Canceled,
        ExecutionType::Canceled,
        "not-expiry",
        0,
        None,
        computed_average(),
    )
    .unwrap();
    assert_eq!(oms.apply_ioc_expiry(not_expiry), Err(Error::ExpiryPolicy));

    assert_eq!(oms.state("gtc").unwrap().status, OrderStatus::Accepted);
    assert_eq!(oms.state("day").unwrap().status, OrderStatus::Accepted);
}

#[test]
fn snapshots_compare_on_visible_fields() {
    let a = OrderSnapshot {
        order: test_order("a"),
        status: OrderStatus::New,
        filled_quantity: 0,
        remaining_quantity: ten(),
        filled_notional: 0,
        average_fill_price: None,
        report_count: 0,
        last_report: None,
    };
    let mut b = a.clone();
    b.order.order_id = "b".to_owned();
    assert!(snapshots_equal(&a, &b));
}
