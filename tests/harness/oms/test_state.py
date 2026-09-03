from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types.accounting import ExecutionReport
from harness.core.types.orders import (
    ExecutionType,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.oms import (
    DuplicateExecutionReportError,
    ExecutionLedgerError,
    IllegalTransitionError,
    OrderStateMachine,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
ZERO = Decimal("0")
TEN = Decimal("10")
UNSET = object()


def order(
    order_id: str = "order-1",
    *,
    order_type: OrderType = OrderType.MARKET,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> SingleLegOrder:
    prices = {
        OrderType.MARKET: (None, None),
        OrderType.LIMIT: (Decimal("101.25"), None),
        OrderType.STOP: (None, Decimal("100.75")),
        OrderType.STOP_LIMIT: (Decimal("101.25"), Decimal("100.75")),
    }
    limit_price, stop_price = prices[order_type]
    return SingleLegOrder(
        event_id=f"event-{order_id}",
        timestamp=STAMP,
        sequence=0,
        order_id=order_id,
        symbol=Symbol("AAPL"),
        side=Side.BUY,
        quantity=TEN,
        order_type=order_type,
        time_in_force=time_in_force,
        limit_price=limit_price,
        stop_price=stop_price,
    )


def report(
    machine: OrderStateMachine,
    status: OrderStatus,
    execution_type: ExecutionType,
    *,
    report_id: str,
    last_quantity: Decimal = ZERO,
    last_price: Decimal | None = None,
    average: object = UNSET,
) -> ExecutionReport:
    snapshot = machine.snapshot
    filled = snapshot.filled_quantity + last_quantity
    notional = snapshot.filled_notional
    if last_price is not None:
        notional += last_quantity * last_price
    calculated_average = None if filled == ZERO else notional / filled
    average_fill_price = calculated_average if average is UNSET else average
    assert average_fill_price is None or isinstance(average_fill_price, Decimal)
    return ExecutionReport(
        event_id=f"event-{report_id}",
        timestamp=STAMP + timedelta(microseconds=snapshot.report_count + 1),
        sequence=snapshot.report_count + 1,
        report_id=report_id,
        order_id=snapshot.order_id,
        status=status,
        execution_type=execution_type,
        order_quantity=snapshot.order.quantity,
        filled_quantity=filled,
        remaining_quantity=snapshot.order.quantity - filled,
        average_fill_price=average_fill_price,
        last_fill_quantity=last_quantity,
        last_fill_price=last_price,
    )


def apply_ack(machine: OrderStateMachine, report_id: str = "ack") -> None:
    machine.apply(
        report(
            machine,
            OrderStatus.ACCEPTED,
            ExecutionType.ACKNOWLEDGED,
            report_id=report_id,
        )
    )


def apply_partial(machine: OrderStateMachine, report_id: str = "partial") -> None:
    machine.apply(
        report(
            machine,
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
            report_id=report_id,
            last_quantity=Decimal("2"),
            last_price=Decimal("100.25"),
        )
    )


def machine_in_status(status: OrderStatus) -> OrderStateMachine:
    machine = OrderStateMachine(order(f"source-{status.value}"))
    if status is OrderStatus.NEW:
        return machine
    if status is OrderStatus.REJECTED:
        machine.apply(
            report(
                machine,
                status,
                ExecutionType.REJECTED,
                report_id="setup-rejected",
            )
        )
        return machine
    apply_ack(machine, "setup-ack")
    if status is OrderStatus.ACCEPTED:
        return machine
    if status is OrderStatus.PARTIALLY_FILLED:
        apply_partial(machine, "setup-partial")
        return machine
    if status is OrderStatus.CANCEL_PENDING:
        machine.apply(
            report(
                machine,
                status,
                ExecutionType.STATUS,
                report_id="setup-cancel-pending",
            )
        )
        return machine
    execution_types = {
        OrderStatus.FILLED: ExecutionType.FILL,
        OrderStatus.CANCELED: ExecutionType.CANCELED,
        OrderStatus.EXPIRED: ExecutionType.EXPIRED,
        OrderStatus.REPLACED: ExecutionType.REPLACED,
    }
    execution_type = execution_types[status]
    last_quantity = TEN if status is OrderStatus.FILLED else ZERO
    last_price = Decimal("100") if status is OrderStatus.FILLED else None
    machine.apply(
        report(
            machine,
            status,
            execution_type,
            report_id=f"setup-{status.value}",
            last_quantity=last_quantity,
            last_price=last_price,
        )
    )
    return machine


def candidate_report(
    machine: OrderStateMachine, target: OrderStatus
) -> ExecutionReport:
    execution_types = {
        OrderStatus.NEW: ExecutionType.STATUS,
        OrderStatus.ACCEPTED: (
            ExecutionType.ACKNOWLEDGED
            if machine.snapshot.status is OrderStatus.NEW
            else ExecutionType.STATUS
        ),
        OrderStatus.PARTIALLY_FILLED: ExecutionType.PARTIAL_FILL,
        OrderStatus.FILLED: ExecutionType.FILL,
        OrderStatus.CANCEL_PENDING: ExecutionType.STATUS,
        OrderStatus.CANCELED: ExecutionType.CANCELED,
        OrderStatus.REJECTED: ExecutionType.REJECTED,
        OrderStatus.EXPIRED: ExecutionType.EXPIRED,
        OrderStatus.REPLACED: ExecutionType.REPLACED,
    }
    last_quantity = ZERO
    last_price = None
    if target is OrderStatus.PARTIALLY_FILLED:
        if machine.snapshot.remaining_quantity > ZERO:
            last_quantity = min(Decimal("1"), machine.snapshot.remaining_quantity)
            last_price = Decimal("100")
    elif target is OrderStatus.FILLED:
        last_quantity = machine.snapshot.remaining_quantity
        if last_quantity > ZERO:
            last_price = Decimal("100")
    return report(
        machine,
        target,
        execution_types[target],
        report_id=f"candidate-{target.value}",
        last_quantity=last_quantity,
        last_price=last_price,
    )


LEGAL_TARGETS = {
    OrderStatus.NEW: {
        OrderStatus.NEW,
        OrderStatus.ACCEPTED,
        OrderStatus.REJECTED,
    },
    OrderStatus.ACCEPTED: {
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
    },
    OrderStatus.CANCEL_PENDING: {
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
    OrderStatus.REPLACED: set(),
}


@pytest.mark.parametrize("source", list(OrderStatus))
@pytest.mark.parametrize("target", list(OrderStatus))
def test_transition_matrix_is_exhaustive(
    source: OrderStatus, target: OrderStatus
) -> None:
    machine = machine_in_status(source)
    next_report = candidate_report(machine, target)

    if target in LEGAL_TARGETS[source]:
        assert machine.apply(next_report).status is target
    else:
        before = machine.snapshot
        with pytest.raises(IllegalTransitionError):
            machine.apply(next_report)
        assert machine.snapshot == before


@pytest.mark.parametrize("order_type", list(OrderType))
@pytest.mark.parametrize("time_in_force", list(TimeInForce))
def test_all_order_types_and_time_in_force_values_share_the_lifecycle(
    order_type: OrderType, time_in_force: TimeInForce
) -> None:
    machine = OrderStateMachine(
        order(
            f"{order_type.value}-{time_in_force.value}",
            order_type=order_type,
            time_in_force=time_in_force,
        )
    )

    apply_ack(machine)
    partial = machine.apply(
        report(
            machine,
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
            report_id="partial",
            last_quantity=Decimal("3"),
            last_price=Decimal("100"),
        )
    )
    filled = machine.apply(
        report(
            machine,
            OrderStatus.FILLED,
            ExecutionType.FILL,
            report_id="fill",
            last_quantity=Decimal("7"),
            last_price=Decimal("101"),
        )
    )

    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert filled.status is OrderStatus.FILLED
    assert filled.filled_quantity == TEN
    assert filled.average_fill_price == Decimal("100.7")
    assert machine.snapshot.order.order_type is order_type
    assert machine.snapshot.time_in_force is time_in_force


def test_partial_fills_and_cancel_preserve_exact_decimal_ledgers() -> None:
    machine = OrderStateMachine(order())
    apply_ack(machine)
    machine.apply(
        report(
            machine,
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
            report_id="partial-1",
            last_quantity=Decimal("0.125"),
            last_price=Decimal("10.01"),
        )
    )
    partial = machine.apply(
        report(
            machine,
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
            report_id="partial-2",
            last_quantity=Decimal("0.375"),
            last_price=Decimal("10.03"),
        )
    )
    machine.apply(
        report(
            machine,
            OrderStatus.CANCEL_PENDING,
            ExecutionType.STATUS,
            report_id="cancel-pending",
        )
    )
    canceled = machine.apply(
        report(
            machine,
            OrderStatus.CANCELED,
            ExecutionType.CANCELED,
            report_id="canceled",
        )
    )

    assert partial.filled_quantity == Decimal("0.500")
    assert partial.remaining_quantity == Decimal("9.500")
    assert partial.filled_notional == Decimal("5.01250")
    assert partial.average_fill_price == Decimal("10.025")
    assert canceled.filled_quantity == partial.filled_quantity
    assert canceled.remaining_quantity == partial.remaining_quantity
    assert canceled.filled_notional == partial.filled_notional
    assert canceled.average_fill_price == partial.average_fill_price


def test_invalid_report_is_atomic_and_duplicate_report_id_is_rejected() -> None:
    machine = OrderStateMachine(order())
    apply_ack(machine)
    before = machine.snapshot
    bad_average = report(
        machine,
        OrderStatus.PARTIALLY_FILLED,
        ExecutionType.PARTIAL_FILL,
        report_id="bad-average",
        last_quantity=Decimal("1"),
        last_price=Decimal("100"),
        average=Decimal("99.99"),
    )

    with pytest.raises(ExecutionLedgerError, match="average_fill_price"):
        machine.apply(bad_average)
    assert machine.snapshot == before
    assert len(machine.reports) == 1

    duplicate = report(
        machine,
        OrderStatus.ACCEPTED,
        ExecutionType.STATUS,
        report_id="ack",
    )
    with pytest.raises(DuplicateExecutionReportError):
        machine.apply(duplicate)


@pytest.mark.parametrize(
    ("execution_type", "status"),
    [
        (ExecutionType.ACKNOWLEDGED, OrderStatus.CANCELED),
        (ExecutionType.PARTIAL_FILL, OrderStatus.FILLED),
        (ExecutionType.FILL, OrderStatus.PARTIALLY_FILLED),
        (ExecutionType.CANCELED, OrderStatus.EXPIRED),
        (ExecutionType.REJECTED, OrderStatus.ACCEPTED),
        (ExecutionType.EXPIRED, OrderStatus.CANCELED),
        (ExecutionType.REPLACED, OrderStatus.CANCELED),
        (ExecutionType.STATUS, OrderStatus.FILLED),
    ],
)
def test_execution_type_cannot_masquerade_as_another_status(
    execution_type: ExecutionType, status: OrderStatus
) -> None:
    machine = OrderStateMachine(order())
    before = machine.snapshot

    with pytest.raises(IllegalTransitionError):
        machine.apply(
            report(
                machine,
                status,
                execution_type,
                report_id="mismatch",
            )
        )

    assert machine.snapshot == before
