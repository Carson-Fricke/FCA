from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types.accounting import Fill
from harness.core.types.orders import (
    MultiLegOrder,
    OrderLeg,
    OrderType,
    Side,
    TimeInForce,
)
from harness.oms.groups import (
    DuplicateGroupError,
    GroupError,
    GroupFillError,
    GroupOrderType,
    GroupStatus,
    GroupTransitionError,
    MultiLegOMS,
    UnknownGroupError,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
ZERO = Decimal("0")
LONG_CALL = Symbol("AAPL240119C00100000")
SHORT_CALL = Symbol("AAPL240119C00105000")


def vertical(
    order_id: str = "vertical",
    *,
    order_type: OrderType = OrderType.LIMIT,
    limit_price: Decimal | None = Decimal("2.10"),
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> MultiLegOrder:
    return MultiLegOrder(
        event_id=f"event-{order_id}",
        timestamp=STAMP,
        sequence=0,
        order_id=order_id,
        legs=(
            OrderLeg(
                LONG_CALL,
                Side.BUY,
                Decimal("2"),
                ratio=Decimal("1"),
                leg_id="long",
            ),
            OrderLeg(
                SHORT_CALL,
                Side.SELL,
                Decimal("2"),
                ratio=Decimal("1"),
                leg_id="short",
            ),
        ),
        order_type=order_type,
        time_in_force=time_in_force,
        limit_price=limit_price,
    )


def fill(
    order_id: str,
    fill_id: str,
    symbol: Symbol,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    *,
    fee: Decimal = ZERO,
    sequence: int = 1,
) -> Fill:
    return Fill(
        event_id=f"event-{fill_id}",
        timestamp=STAMP + timedelta(microseconds=sequence),
        sequence=sequence,
        fill_id=fill_id,
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
    )


def debit_fills(order_id: str = "vertical") -> tuple[Fill, Fill]:
    return (
        fill(
            order_id,
            "long-fill",
            LONG_CALL,
            Side.BUY,
            Decimal("2"),
            Decimal("4.125"),
            fee=Decimal("0.13"),
        ),
        fill(
            order_id,
            "short-fill",
            SHORT_CALL,
            Side.SELL,
            Decimal("2"),
            Decimal("2.075"),
            fee=Decimal("0.11"),
            sequence=2,
        ),
    )


def working_oms(
    order: MultiLegOrder | None = None,
    order_type: GroupOrderType = GroupOrderType.NET_DEBIT,
) -> MultiLegOMS:
    oms = MultiLegOMS()
    submitted = vertical() if order is None else order
    oms.submit(submitted, order_type)
    oms.accept(submitted.order_id)
    return oms


def test_filled_vertical_has_exact_leg_positions_and_cash_including_fees() -> None:
    oms = working_oms()
    evaluating = oms.begin_evaluation("vertical")

    # Matching is by exact leg contract rather than caller-provided fill order.
    filled = oms.fill("vertical", tuple(reversed(debit_fills())))

    assert evaluating.status is GroupStatus.EVALUATING
    assert tuple(leg.status for leg in filled.legs) == (
        GroupStatus.FILLED,
        GroupStatus.FILLED,
    )
    assert tuple(leg.action for leg in filled.legs) == (Side.BUY, Side.SELL)
    assert tuple(leg.ratio for leg in filled.legs) == (Decimal("1"), Decimal("1"))
    assert filled.quantity == Decimal("2")
    assert tuple(leg.position_quantity for leg in filled.legs) == (
        Decimal("2"),
        Decimal("-2"),
    )
    assert tuple(leg.filled_notional for leg in filled.legs) == (
        Decimal("8.250"),
        Decimal("4.150"),
    )
    assert tuple(leg.net_cash for leg in filled.legs) == (
        Decimal("-8.380"),
        Decimal("4.040"),
    )
    assert filled.net_price == Decimal("2.050")
    assert filled.net_debit == Decimal("2.050")
    assert filled.net_credit == ZERO
    assert filled.gross_cash == Decimal("-4.100")
    assert filled.fees == Decimal("0.24")
    assert filled.net_cash == Decimal("-4.340")
    assert filled.fills == debit_fills()
    assert oms.fills("vertical") == debit_fills()


def test_market_group_fills_atomically_without_a_net_direction_constraint() -> None:
    market = vertical(order_id="market", order_type=OrderType.MARKET, limit_price=None)
    oms = MultiLegOMS()
    submitted = oms.submit(market)
    oms.accept("market")
    oms.begin_evaluation("market")
    fills = (
        fill("market", "market-long", LONG_CALL, Side.BUY, Decimal("2"), Decimal("3")),
        fill(
            "market",
            "market-short",
            SHORT_CALL,
            Side.SELL,
            Decimal("2"),
            Decimal("4"),
            sequence=2,
        ),
    )

    state = oms.fill("market", fills)

    assert submitted.order_type is GroupOrderType.MARKET
    assert state.status is GroupStatus.FILLED
    assert state.net_price == Decimal("-1")
    assert state.net_credit == Decimal("1")
    assert state.net_cash == Decimal("2")


def test_net_credit_requires_the_minimum_ratio_weighted_credit() -> None:
    credit = MultiLegOrder(
        event_id="event-credit",
        timestamp=STAMP,
        sequence=0,
        order_id="credit",
        legs=(
            OrderLeg(LONG_CALL, Side.SELL, Decimal("2"), ratio=Decimal("2")),
            OrderLeg(SHORT_CALL, Side.BUY, Decimal("1"), ratio=Decimal("1")),
        ),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("3.00"),
    )
    oms = working_oms(credit, GroupOrderType.NET_CREDIT)
    oms.begin_evaluation("credit")
    candidate = (
        fill("credit", "credit-sell", LONG_CALL, Side.SELL, Decimal("2"), Decimal("4")),
        fill(
            "credit",
            "credit-buy",
            SHORT_CALL,
            Side.BUY,
            Decimal("1"),
            Decimal("5"),
            sequence=2,
        ),
    )

    state = oms.fill("credit", candidate)

    assert state.net_price == Decimal("-3")
    assert state.net_credit == Decimal("3")
    assert state.net_cash == Decimal("3")


@pytest.mark.parametrize(
    ("order_type", "limit_price", "prices", "message"),
    [
        (
            GroupOrderType.NET_DEBIT,
            Decimal("2.00"),
            (Decimal("4.125"), Decimal("2.075")),
            "exceeds",
        ),
        (
            GroupOrderType.NET_CREDIT,
            Decimal("2.25"),
            (Decimal("4"), Decimal("2")),
            "below",
        ),
    ],
)
def test_failed_net_limit_evaluation_is_atomic_and_can_be_retried(
    order_type: GroupOrderType,
    limit_price: Decimal,
    prices: tuple[Decimal, Decimal],
    message: str,
) -> None:
    order = vertical(limit_price=limit_price)
    if order_type is GroupOrderType.NET_CREDIT:
        order = MultiLegOrder(
            event_id=order.event_id,
            timestamp=order.timestamp,
            sequence=order.sequence,
            order_id=order.order_id,
            legs=(
                OrderLeg(LONG_CALL, Side.SELL, Decimal("2")),
                OrderLeg(SHORT_CALL, Side.BUY, Decimal("2")),
            ),
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
        )
    oms = working_oms(order, order_type)
    oms.begin_evaluation("vertical")
    candidate = tuple(
        fill(
            "vertical",
            f"candidate-{index}",
            leg.symbol,
            leg.side,
            leg.quantity,
            price,
            sequence=index + 1,
        )
        for index, (leg, price) in enumerate(zip(order.legs, prices, strict=True))
    )
    before = oms.state("vertical")

    with pytest.raises(GroupFillError, match=message):
        oms.fill("vertical", candidate)

    assert oms.state("vertical") == before
    assert oms.fills("vertical") == ()


@pytest.mark.parametrize(
    "bad_fills",
    [
        debit_fills()[:1],
        (
            fill(
                "vertical",
                "partial-long",
                LONG_CALL,
                Side.BUY,
                Decimal("1"),
                Decimal("4"),
            ),
            debit_fills()[1],
        ),
        (
            debit_fills()[0],
            fill(
                "other-order",
                "wrong-order",
                SHORT_CALL,
                Side.SELL,
                Decimal("2"),
                Decimal("2"),
            ),
        ),
    ],
)
def test_partial_or_mismatched_group_fills_are_prohibited_atomically(
    bad_fills: tuple[Fill, ...],
) -> None:
    oms = working_oms()
    oms.begin_evaluation("vertical")
    before = oms.state("vertical")

    with pytest.raises(GroupFillError):
        oms.fill("vertical", bad_fills)

    assert oms.state("vertical") == before
    assert all(leg.filled_quantity == ZERO for leg in before.legs)
    assert oms.fills("vertical") == ()


def test_cancel_during_evaluation_propagates_and_blocks_stale_fill_batch() -> None:
    oms = working_oms()
    oms.begin_evaluation("vertical")

    canceled = oms.cancel("vertical", "strategy withdrew the spread")

    assert canceled.status is GroupStatus.CANCELED
    assert canceled.reason == "strategy withdrew the spread"
    assert all(leg.status is GroupStatus.CANCELED for leg in canceled.legs)
    assert all(leg.filled_quantity == ZERO for leg in canceled.legs)
    with pytest.raises(GroupTransitionError, match="canceled state"):
        oms.fill("vertical", debit_fills())
    assert oms.state("vertical") == canceled


def test_rejection_and_expiry_propagate_without_leg_accounting() -> None:
    oms = MultiLegOMS()
    oms.submit(vertical("rejected"), GroupOrderType.NET_DEBIT)
    rejected = oms.reject("rejected", "unsupported contract")
    oms.submit(vertical("expired"), GroupOrderType.NET_DEBIT)
    oms.accept("expired")
    oms.begin_evaluation("expired")
    expired = oms.expire("expired", "session ended")

    assert rejected.status is GroupStatus.REJECTED
    assert rejected.reason == "unsupported contract"
    assert all(leg.status is GroupStatus.REJECTED for leg in rejected.legs)
    assert expired.status is GroupStatus.EXPIRED
    assert expired.reason == "session ended"
    assert all(leg.status is GroupStatus.EXPIRED for leg in expired.legs)
    assert all(leg.remaining_quantity == leg.quantity for leg in expired.legs)
    assert all(leg.net_cash == ZERO for leg in (*rejected.legs, *expired.legs))
    assert oms.active_states() == ()


def test_unfilled_evaluation_returns_to_working_and_terminal_states_are_final() -> None:
    oms = working_oms()

    oms.begin_evaluation("vertical")
    accepted = oms.end_evaluation("vertical")
    expired = oms.expire("vertical")

    assert accepted.status is GroupStatus.ACCEPTED
    assert all(leg.status is GroupStatus.ACCEPTED for leg in accepted.legs)
    assert expired.is_terminal
    with pytest.raises(GroupTransitionError):
        oms.begin_evaluation("vertical")
    with pytest.raises(GroupTransitionError):
        oms.cancel("vertical")


def test_group_type_identity_and_registry_validation() -> None:
    oms = MultiLegOMS()
    limit = vertical()

    with pytest.raises(GroupError, match="requires NET_DEBIT or NET_CREDIT"):
        oms.submit(limit)
    with pytest.raises(GroupError, match="net price"):
        oms.submit(limit, GroupOrderType.MARKET)
    with pytest.raises(GroupError, match="requires MARKET"):
        oms.submit(
            vertical(order_id="market", order_type=OrderType.MARKET, limit_price=None),
            GroupOrderType.NET_DEBIT,
        )
    with pytest.raises(GroupError, match="proportional"):
        oms.submit(
            MultiLegOrder(
                event_id="bad-ratio-event",
                timestamp=STAMP,
                sequence=0,
                order_id="bad-ratio",
                legs=(
                    OrderLeg(LONG_CALL, Side.BUY, Decimal("1"), ratio=Decimal("2")),
                    OrderLeg(SHORT_CALL, Side.SELL, Decimal("1")),
                ),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("1"),
            ),
            GroupOrderType.NET_DEBIT,
        )

    oms.submit(limit, GroupOrderType.NET_DEBIT)
    with pytest.raises(DuplicateGroupError):
        oms.submit(limit, GroupOrderType.NET_DEBIT)
    with pytest.raises(UnknownGroupError):
        oms.state("missing")
