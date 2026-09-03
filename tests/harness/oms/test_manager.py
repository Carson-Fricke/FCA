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
    ExecutionLedgerError,
    OmsError,
    ReplaceError,
    ReplaceStatus,
    SessionAction,
    SessionDirective,
    SingleLegOMS,
    UnknownOrderError,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
ZERO = Decimal("0")
AAPL = Symbol("AAPL")
UNSET = object()


def order(
    order_id: str,
    *,
    quantity: Decimal = Decimal("10"),
    time_in_force: TimeInForce = TimeInForce.DAY,
    side: Side = Side.BUY,
    symbol: Symbol = AAPL,
    sequence: int = 0,
) -> SingleLegOrder:
    return SingleLegOrder(
        event_id=f"event-{order_id}",
        timestamp=STAMP + timedelta(seconds=sequence),
        sequence=sequence,
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        time_in_force=time_in_force,
        limit_price=Decimal("101.25"),
    )


def report(
    oms: SingleLegOMS,
    order_id: str,
    status: OrderStatus,
    execution_type: ExecutionType,
    report_id: str,
    *,
    last_quantity: Decimal = ZERO,
    last_price: Decimal | None = None,
    average: object = UNSET,
) -> ExecutionReport:
    state = oms.state(order_id)
    filled = state.filled_quantity + last_quantity
    notional = state.filled_notional
    if last_price is not None:
        notional += last_quantity * last_price
    calculated_average = None if filled == ZERO else notional / filled
    average_fill_price = calculated_average if average is UNSET else average
    assert average_fill_price is None or isinstance(average_fill_price, Decimal)
    return ExecutionReport(
        event_id=f"event-{report_id}",
        timestamp=STAMP + timedelta(microseconds=state.report_count + 1),
        sequence=state.report_count + 1,
        report_id=report_id,
        order_id=order_id,
        status=status,
        execution_type=execution_type,
        order_quantity=state.order.quantity,
        filled_quantity=filled,
        remaining_quantity=state.order.quantity - filled,
        average_fill_price=average_fill_price,
        last_fill_quantity=last_quantity,
        last_fill_price=last_price,
    )


def acknowledge(oms: SingleLegOMS, order_id: str) -> None:
    oms.apply(
        report(
            oms,
            order_id,
            OrderStatus.ACCEPTED,
            ExecutionType.ACKNOWLEDGED,
            f"ack-{order_id}",
        )
    )


def expire_report(oms: SingleLegOMS, order_id: str) -> ExecutionReport:
    return report(
        oms,
        order_id,
        OrderStatus.EXPIRED,
        ExecutionType.EXPIRED,
        f"expired-{order_id}",
    )


def test_replace_is_reserved_then_confirmed_atomically_by_report() -> None:
    oms = SingleLegOMS()
    oms.submit(order("original"))
    acknowledge(oms, "original")
    oms.apply(
        report(
            oms,
            "original",
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
            "partial-original",
            last_quantity=Decimal("1.25"),
            last_price=Decimal("100.10"),
        )
    )
    replacement = order("replacement", quantity=Decimal("6"), sequence=10)

    pending = oms.request_replace("original", replacement)

    assert pending.status is ReplaceStatus.PENDING
    assert oms.state("original").status is OrderStatus.PARTIALLY_FILLED
    assert oms.replaced_by("original") is None
    with pytest.raises(UnknownOrderError):
        oms.state("replacement")

    replaced = oms.apply(
        report(
            oms,
            "original",
            OrderStatus.REPLACED,
            ExecutionType.REPLACED,
            "replaced-original",
        )
    )

    assert replaced.status is OrderStatus.REPLACED
    assert replaced.filled_quantity == Decimal("1.25")
    assert replaced.average_fill_price == Decimal("100.10")
    assert oms.state("replacement").status is OrderStatus.NEW
    assert oms.replaced_by("original") == "replacement"
    assert oms.replaces("replacement") == "original"
    assert oms.replacement_chain("replacement") == ("original", "replacement")
    applied = oms.replacement("original")
    assert applied is not None
    assert applied.status is ReplaceStatus.APPLIED
    assert applied.report_id == "replaced-original"
    assert tuple(state.order_id for state in oms.active_states()) == ("replacement",)


def test_failed_replace_report_changes_nothing_and_keeps_reservation() -> None:
    oms = SingleLegOMS()
    oms.submit(order("original"))
    acknowledge(oms, "original")
    oms.apply(
        report(
            oms,
            "original",
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
            "partial",
            last_quantity=Decimal("1"),
            last_price=Decimal("100"),
        )
    )
    oms.request_replace("original", order("replacement"))
    before = oms.state("original")
    invalid = report(
        oms,
        "original",
        OrderStatus.REPLACED,
        ExecutionType.REPLACED,
        "bad-replaced",
        average=Decimal("99"),
    )

    with pytest.raises(ExecutionLedgerError):
        oms.apply(invalid)

    assert oms.state("original") == before
    pending = oms.replacement("original")
    assert pending is not None
    assert pending.status is ReplaceStatus.PENDING
    with pytest.raises(UnknownOrderError):
        oms.state("replacement")


def test_replace_requires_accepted_compatible_order_and_pending_request() -> None:
    oms = SingleLegOMS()
    oms.submit(order("original"))

    with pytest.raises(ReplaceError, match="new state"):
        oms.request_replace("original", order("too-early"))

    acknowledge(oms, "original")
    with pytest.raises(ReplaceError, match="symbol"):
        oms.request_replace("original", order("wrong-symbol", symbol=Symbol("MSFT")))
    with pytest.raises(ReplaceError, match="side"):
        oms.request_replace("original", order("wrong-side", side=Side.SELL))

    without_pending = report(
        oms,
        "original",
        OrderStatus.REPLACED,
        ExecutionType.REPLACED,
        "without-pending",
    )
    with pytest.raises(ReplaceError, match="no pending"):
        oms.apply(without_pending)
    assert oms.state("original").status is OrderStatus.ACCEPTED


def test_terminal_report_aborts_pending_replace_and_preserves_cancel_ledger() -> None:
    oms = SingleLegOMS()
    oms.submit(order("original"))
    acknowledge(oms, "original")
    oms.request_replace("original", order("reserved"))

    oms.apply(
        report(
            oms,
            "original",
            OrderStatus.CANCEL_PENDING,
            ExecutionType.STATUS,
            "cancel-pending",
        )
    )
    canceled = oms.apply(
        report(
            oms,
            "original",
            OrderStatus.CANCELED,
            ExecutionType.CANCELED,
            "canceled",
        )
    )

    assert canceled.filled_quantity == ZERO
    assert canceled.remaining_quantity == Decimal("10")
    replacement = oms.replacement("original")
    assert replacement is not None
    assert replacement.status is ReplaceStatus.ABORTED
    assert replacement.report_id == "canceled"
    with pytest.raises(UnknownOrderError):
        oms.state("reserved")


def test_ioc_expiry_and_gtc_session_carry_over() -> None:
    oms = SingleLegOMS()
    oms.submit(order("local-new", time_in_force=TimeInForce.GTC))
    oms.submit(order("day", time_in_force=TimeInForce.DAY))
    oms.submit(order("gtc", time_in_force=TimeInForce.GTC))
    oms.submit(order("ioc", time_in_force=TimeInForce.IOC))
    for order_id in ("day", "gtc", "ioc"):
        acknowledge(oms, order_id)

    assert oms.session_directives() == (
        SessionDirective("day", SessionAction.EXPIRE),
        SessionDirective("gtc", SessionAction.CARRY),
        SessionDirective("ioc", SessionAction.EXPIRE),
    )
    assert oms.ioc_expiry_directives() == (
        SessionDirective("ioc", SessionAction.EXPIRE),
    )

    oms.apply(
        report(
            oms,
            "ioc",
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
            "ioc-partial",
            last_quantity=Decimal("2.5"),
            last_price=Decimal("100.05"),
        )
    )
    ioc_expired = oms.apply_ioc_expiry(expire_report(oms, "ioc"))
    day_expired = oms.apply_session_expiry(expire_report(oms, "day"))

    assert ioc_expired.status is OrderStatus.EXPIRED
    assert ioc_expired.filled_quantity == Decimal("2.5")
    assert ioc_expired.remaining_quantity == Decimal("7.5")
    assert ioc_expired.average_fill_price == Decimal("100.05")
    assert day_expired.status is OrderStatus.EXPIRED
    assert oms.state("gtc").status is OrderStatus.ACCEPTED
    assert oms.state("local-new").status is OrderStatus.NEW
    assert oms.session_directives() == (SessionDirective("gtc", SessionAction.CARRY),)

    # A later-session report continues the same GTC ledger.
    filled = oms.apply(
        report(
            oms,
            "gtc",
            OrderStatus.FILLED,
            ExecutionType.FILL,
            "gtc-next-session-fill",
            last_quantity=Decimal("10"),
            last_price=Decimal("102.15"),
        )
    )
    assert filled.status is OrderStatus.FILLED
    assert filled.average_fill_price == Decimal("102.15")


def test_expiry_policy_refuses_wrong_tif_and_non_expiry_reports() -> None:
    oms = SingleLegOMS()
    oms.submit(order("gtc", time_in_force=TimeInForce.GTC))
    oms.submit(order("day", time_in_force=TimeInForce.DAY))
    acknowledge(oms, "gtc")
    acknowledge(oms, "day")

    with pytest.raises(OmsError, match="carries"):
        oms.apply_session_expiry(expire_report(oms, "gtc"))
    with pytest.raises(OmsError, match="IOC order"):
        oms.apply_ioc_expiry(expire_report(oms, "day"))
    with pytest.raises(OmsError, match="expired execution report"):
        oms.apply_ioc_expiry(
            report(
                oms,
                "day",
                OrderStatus.CANCELED,
                ExecutionType.CANCELED,
                "not-expiry",
            )
        )

    assert oms.state("gtc").status is OrderStatus.ACCEPTED
    assert oms.state("day").status is OrderStatus.ACCEPTED
