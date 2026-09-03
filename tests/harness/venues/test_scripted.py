from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types.accounting import ExecutionReport, Fill, Position
from harness.core.types.events import Event
from harness.core.types.orders import (
    ExecutionType,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
)
from harness.oms.manager import SingleLegOMS
from harness.venues.base import ExecutionVenue
from harness.venues.scripted import (
    ScriptedStep,
    ScriptedVenue,
    UnexpectedVenueCall,
    VenueCall,
    VenueOperation,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
ZERO = Decimal("0")
QUANTITY = Decimal("10")
PRICE = Decimal("100.25")
AAPL = Symbol("AAPL")


def order(order_id: str, *, quantity: Decimal = QUANTITY) -> SingleLegOrder:
    return SingleLegOrder(
        event_id=f"event-{order_id}",
        timestamp=STAMP,
        sequence=0,
        order_id=order_id,
        symbol=AAPL,
        side=Side.BUY,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("101"),
    )


def report(
    order_id: str,
    name: str,
    offset: int,
    status: OrderStatus,
    execution_type: ExecutionType,
    *,
    order_quantity: Decimal = QUANTITY,
    filled_quantity: Decimal = ZERO,
    last_fill_quantity: Decimal = ZERO,
    last_fill_price: Decimal | None = None,
) -> ExecutionReport:
    average_fill_price = None if filled_quantity == ZERO else PRICE
    return ExecutionReport(
        event_id=f"event-{name}",
        timestamp=STAMP + timedelta(microseconds=offset),
        sequence=offset,
        report_id=name,
        order_id=order_id,
        status=status,
        execution_type=execution_type,
        order_quantity=order_quantity,
        filled_quantity=filled_quantity,
        remaining_quantity=order_quantity - filled_quantity,
        average_fill_price=average_fill_price,
        last_fill_quantity=last_fill_quantity,
        last_fill_price=last_fill_price,
    )


def fill(
    order_id: str,
    name: str,
    offset: int,
    quantity: Decimal,
) -> Fill:
    return Fill(
        event_id=f"event-{name}",
        timestamp=STAMP + timedelta(microseconds=offset),
        sequence=offset,
        fill_id=name,
        order_id=order_id,
        symbol=AAPL,
        side=Side.BUY,
        quantity=quantity,
        price=PRICE,
    )


def apply_reports(bus: EventBus, oms: SingleLegOMS) -> None:
    def apply_report(event: ExecutionReport) -> None:
        oms.apply(event)

    bus.subscribe(ExecutionReport, apply_report)


def test_toy_submit_ack_partial_fill_and_fill_callback_ordering() -> None:
    bus = EventBus(SimClock(STAMP))
    oms = SingleLegOMS()
    submitted = order("toy")
    partial_quantity = Decimal("3")
    final_quantity = QUANTITY - partial_quantity
    acknowledgement = report(
        "toy", "ack", 1, OrderStatus.ACCEPTED, ExecutionType.ACKNOWLEDGED
    )
    partial_report = report(
        "toy",
        "partial",
        2,
        OrderStatus.PARTIALLY_FILLED,
        ExecutionType.PARTIAL_FILL,
        filled_quantity=partial_quantity,
        last_fill_quantity=partial_quantity,
        last_fill_price=PRICE,
    )
    partial_fill = fill("toy", "partial-fill", 2, partial_quantity)
    final_report = report(
        "toy",
        "filled",
        3,
        OrderStatus.FILLED,
        ExecutionType.FILL,
        filled_quantity=QUANTITY,
        last_fill_quantity=final_quantity,
        last_fill_price=PRICE,
    )
    final_fill = fill("toy", "final-fill", 3, final_quantity)
    venue = ScriptedVenue(
        bus,
        [
            ScriptedStep(
                VenueCall(VenueOperation.SUBMIT, order_id="toy"),
                (
                    acknowledgement,
                    partial_fill,
                    partial_report,
                    final_fill,
                    final_report,
                ),
            )
        ],
    )
    callbacks: list[str] = []

    def on_order(event: SingleLegOrder) -> None:
        callbacks.append("strategy:submit:start")
        oms.submit(event)
        result = venue.submit(event)
        assert result is None
        callbacks.append("strategy:submit:end")

    def on_report(event: ExecutionReport) -> None:
        callbacks.append(f"report:{event.report_id}")
        oms.apply(event)

    bus.subscribe(SingleLegOrder, on_order)
    bus.subscribe(ExecutionReport, on_report)
    bus.subscribe(Fill, lambda event: callbacks.append(f"fill:{event.fill_id}"))
    bus.publish(submitted)

    dispatched = bus.drain()

    assert isinstance(venue, ExecutionVenue)
    assert [event.event_id for event in dispatched] == [
        "event-toy",
        "event-ack",
        "event-partial",
        "event-partial-fill",
        "event-filled",
        "event-final-fill",
    ]
    assert callbacks == [
        "strategy:submit:start",
        "strategy:submit:end",
        "report:ack",
        "report:partial",
        "fill:partial-fill",
        "report:filled",
        "fill:final-fill",
    ]
    assert oms.state("toy").status is OrderStatus.FILLED
    assert oms.state("toy").average_fill_price == PRICE
    assert venue.calls == (VenueCall(VenueOperation.SUBMIT, order_id="toy"),)
    venue.assert_exhausted()


@pytest.mark.parametrize(
    ("name", "outcomes", "expected_status"),
    [
        (
            "rejection",
            (
                report(
                    "rejection",
                    "rejected",
                    1,
                    OrderStatus.REJECTED,
                    ExecutionType.REJECTED,
                ),
            ),
            OrderStatus.REJECTED,
        ),
        (
            "expiry",
            (
                report(
                    "expiry",
                    "expiry-ack",
                    1,
                    OrderStatus.ACCEPTED,
                    ExecutionType.ACKNOWLEDGED,
                ),
                report(
                    "expiry",
                    "expired",
                    2,
                    OrderStatus.EXPIRED,
                    ExecutionType.EXPIRED,
                ),
            ),
            OrderStatus.EXPIRED,
        ),
        (
            "immediate",
            (
                report(
                    "immediate",
                    "immediate-ack",
                    1,
                    OrderStatus.ACCEPTED,
                    ExecutionType.ACKNOWLEDGED,
                ),
                report(
                    "immediate",
                    "immediate-fill-report",
                    1,
                    OrderStatus.FILLED,
                    ExecutionType.FILL,
                    filled_quantity=QUANTITY,
                    last_fill_quantity=QUANTITY,
                    last_fill_price=PRICE,
                ),
                fill("immediate", "immediate-fill", 1, QUANTITY),
            ),
            OrderStatus.FILLED,
        ),
    ],
)
def test_scripted_submission_terminal_scenarios(
    name: str,
    outcomes: tuple[Event, ...],
    expected_status: OrderStatus,
) -> None:
    bus = EventBus(SimClock(STAMP))
    oms = SingleLegOMS()
    submitted = order(name)
    oms.submit(submitted)
    apply_reports(bus, oms)
    venue = ScriptedVenue(
        bus,
        [
            ScriptedStep(
                VenueCall(VenueOperation.SUBMIT, order_id=name),
                outcomes,
            )
        ],
    )

    assert venue.submit(submitted) is None
    dispatched = bus.drain()

    assert oms.state(name).status is expected_status
    assert tuple(dispatched) == outcomes
    venue.assert_exhausted()


def test_cancellation_and_replacement_scenarios() -> None:
    bus = EventBus(SimClock(STAMP))
    oms = SingleLegOMS()
    canceled_order = order("cancel-me")
    original = order("original")
    replacement = order("replacement", quantity=Decimal("6"))
    for submitted in (canceled_order, original):
        oms.submit(submitted)
    apply_reports(bus, oms)
    cancel_ack = report(
        "cancel-me",
        "cancel-ack",
        1,
        OrderStatus.ACCEPTED,
        ExecutionType.ACKNOWLEDGED,
    )
    original_ack = report(
        "original",
        "original-ack",
        2,
        OrderStatus.ACCEPTED,
        ExecutionType.ACKNOWLEDGED,
    )
    canceled = report(
        "cancel-me",
        "canceled",
        3,
        OrderStatus.CANCELED,
        ExecutionType.CANCELED,
    )
    replaced = report(
        "original",
        "replaced",
        4,
        OrderStatus.REPLACED,
        ExecutionType.REPLACED,
    )
    venue = ScriptedVenue(
        bus,
        [
            ScriptedStep(
                VenueCall(VenueOperation.SUBMIT, order_id="cancel-me"),
                (cancel_ack,),
            ),
            ScriptedStep(
                VenueCall(VenueOperation.SUBMIT, order_id="original"),
                (original_ack,),
            ),
            ScriptedStep(
                VenueCall(VenueOperation.CANCEL, order_id="cancel-me"),
                (canceled,),
            ),
            ScriptedStep(
                VenueCall(
                    VenueOperation.REPLACE,
                    order_id="original",
                    replacement_order_id="replacement",
                ),
                (replaced,),
            ),
        ],
    )

    venue.submit(canceled_order)
    venue.submit(original)
    bus.drain()
    oms.request_replace("original", replacement)
    assert venue.cancel("cancel-me") is None
    assert venue.replace("original", replacement) is None
    bus.drain()

    assert oms.state("cancel-me").status is OrderStatus.CANCELED
    assert oms.state("original").status is OrderStatus.REPLACED
    assert oms.state("replacement").status is OrderStatus.NEW
    assert oms.replaced_by("original") == "replacement"
    venue.assert_exhausted()


def test_position_snapshot_is_delivered_only_through_bus() -> None:
    bus = EventBus(SimClock(STAMP))
    position = Position(
        event_id="event-position",
        timestamp=STAMP,
        sequence=0,
        account_id="account-1",
        symbol=AAPL,
        quantity=Decimal("4"),
        average_price=Decimal("99"),
    )
    venue = ScriptedVenue(
        bus,
        [
            ScriptedStep(
                VenueCall(
                    VenueOperation.POSITION_SNAPSHOT,
                    account_id="account-1",
                ),
                (position,),
            )
        ],
    )
    received: list[Position] = []
    bus.subscribe(Position, received.append)

    result = venue.request_position_snapshot("account-1")

    assert result is None
    assert received == []
    assert bus.drain() == (position,)
    assert received == [position]


def test_script_mismatch_does_not_consume_expected_step() -> None:
    bus = EventBus(SimClock(STAMP))
    expected = VenueCall(VenueOperation.SUBMIT, order_id="expected")
    venue = ScriptedVenue(bus, [ScriptedStep(expected)])

    with pytest.raises(UnexpectedVenueCall, match="expected"):
        venue.submit(order("wrong"))

    assert venue.calls == ()
    assert venue.remaining_steps == 1
    with pytest.raises(AssertionError, match="1 scripted venue call"):
        venue.assert_exhausted()


def test_script_records_no_unscripted_call_or_outcome() -> None:
    venue = ScriptedVenue(EventBus(SimClock(STAMP)))

    with pytest.raises(UnexpectedVenueCall, match="no scripted calls remain"):
        venue.cancel("unknown")

    assert venue.calls == ()
