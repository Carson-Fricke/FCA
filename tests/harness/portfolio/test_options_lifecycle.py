from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from harness.core.clock import Clock, SimClock
from harness.core.symbols import OsiSymbol, Symbol
from harness.core.types.accounting import Fill
from harness.core.types.orders import Side
from harness.portfolio import (
    AssignmentPolicy,
    DaysToExpiry,
    ExpiredContractError,
    InvalidAssignmentError,
    MissingUnderlyingPriceError,
    OptionLifecycle,
    PessimisticAssignment,
    PortfolioHub,
    SettlementKind,
)

STAMP = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
EXPIRY = datetime(2024, 1, 19, 20, 0, tzinfo=UTC)
AAPL = Symbol("AAPL")
PUT_90 = OsiSymbol("AAPL240119P00090000")
PUT_100 = OsiSymbol("AAPL240119P00100000")
CALL_100 = OsiSymbol("AAPL240119C00100000")
CALL_150 = OsiSymbol("AAPL240119C00150000")
ZERO = Decimal("0")


def fill(
    fill_id: str,
    side: Side,
    symbol: Symbol,
    quantity: Decimal,
    price: Decimal,
    *,
    fee: Decimal = ZERO,
    sequence: int = 0,
) -> Fill:
    return Fill(
        event_id=f"event-{fill_id}",
        timestamp=STAMP + timedelta(microseconds=sequence),
        sequence=sequence,
        fill_id=fill_id,
        order_id=f"order-{fill_id}",
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
    )


def open_lifecycle(
    initial_cash: Decimal = Decimal("10000"),
) -> tuple[PortfolioHub, SimClock, OptionLifecycle]:
    hub = PortfolioHub("paper-account")
    hub.create_book("alpha", initial_cash)
    clock = SimClock(STAMP)
    return hub, clock, OptionLifecycle(hub, clock)


def dte_records(events: tuple[DaysToExpiry, ...]) -> list[tuple[OsiSymbol, int, int]]:
    return [(event.symbol, event.threshold, event.days_to_expiry) for event in events]


def test_itm_short_put_spread_settles_exact_assignment_exercise_and_cash() -> None:
    hub, clock, lifecycle = open_lifecycle()
    lifecycle.apply_fill(
        "alpha", fill("long-put", Side.BUY, PUT_100, Decimal("1"), Decimal("300"))
    )
    lifecycle.apply_fill(
        "alpha",
        fill("short-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"), sequence=1),
    )
    assert lifecycle.open_contracts == (PUT_90, PUT_100)
    assert hub.cash == Decimal("9800")

    clock.advance_to(EXPIRY)
    settlements = lifecycle.settle_expired({"AAPL": "85"})

    assert [settlement.symbol for settlement in settlements] == [PUT_90, PUT_100]
    assignment = settlements[0]
    exercise = settlements[1]
    assert assignment.kind is SettlementKind.ASSIGNED
    assert assignment.contracts == Decimal("1")
    assert assignment.spot == Decimal("85")
    assert assignment.settlement_value == ZERO
    assert assignment.underlying_fill is not None
    assert assignment.underlying_fill.fill_id == f"settle:{PUT_90.value}:alpha"
    assert assignment.underlying_fill.side is Side.BUY
    assert assignment.underlying_fill.quantity == Decimal("100")
    assert assignment.underlying_fill.price == Decimal("90.000")
    assert exercise.kind is SettlementKind.EXERCISED
    assert exercise.contracts == Decimal("1")
    assert exercise.settlement_value == ZERO
    assert exercise.underlying_fill is not None
    assert exercise.underlying_fill.side is Side.SELL
    assert exercise.underlying_fill.quantity == Decimal("100")
    assert exercise.underlying_fill.price == Decimal("100.000")

    book = hub.book("alpha")
    assert book.position(PUT_90).is_flat
    assert book.position(PUT_90).realized_pnl == Decimal("100")
    assert book.position(PUT_100).is_flat
    assert book.position(PUT_100).realized_pnl == Decimal("-300")
    assert book.position(AAPL).is_flat
    assert book.position(AAPL).realized_pnl == Decimal("1000")
    assert hub.cash == Decimal("10800")
    assert hub.realized_pnl == Decimal("800")
    assert hub.equity == Decimal("10800")
    assert hub.total_pnl == Decimal("800")
    assert lifecycle.open_contracts == ()
    assert lifecycle.retired_symbols == (PUT_90, PUT_100)
    assert lifecycle.is_retired(PUT_90)
    assert lifecycle.is_retired(PUT_100)


def test_itm_short_put_assignment_opens_exact_underlying_position() -> None:
    hub, clock, lifecycle = open_lifecycle()
    lifecycle.apply_fill(
        "alpha", fill("short-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"))
    )

    clock.advance_to(EXPIRY)
    (assignment,) = lifecycle.settle_expired({"AAPL": "85"})

    assert assignment.kind is SettlementKind.ASSIGNED
    book = hub.book("alpha")
    shares = book.position(AAPL)
    assert shares.quantity == Decimal("100")
    assert shares.cost_basis == Decimal("9000")
    assert shares.average_cost == Decimal("90")
    assert shares.realized_pnl == ZERO
    assert book.position(PUT_90).is_flat
    assert book.position(PUT_90).realized_pnl == Decimal("100")
    assert hub.cash == Decimal("1100")
    assert hub.realized_pnl == Decimal("100")
    assert hub.equity is None

    hub.mark(AAPL, "85")
    shares = book.position(AAPL)
    assert shares.unrealized_pnl == Decimal("-500")
    assert hub.cash == Decimal("1100")
    assert hub.equity == Decimal("9600")
    assert hub.total_pnl == Decimal("-400")


def test_worthless_and_pinned_expiry_retire_without_cash_effects() -> None:
    hub, clock, lifecycle = open_lifecycle()
    lifecycle.apply_fill(
        "alpha", fill("long-call", Side.BUY, CALL_150, Decimal("1"), Decimal("200"))
    )
    lifecycle.apply_fill(
        "alpha",
        fill("short-put", Side.SELL, PUT_100, Decimal("1"), Decimal("150"), sequence=1),
    )

    clock.advance_to(EXPIRY)
    settlements = lifecycle.settle_expired({"AAPL": "100"})

    assert [settlement.symbol for settlement in settlements] == [CALL_150, PUT_100]
    assert all(
        settlement.kind is SettlementKind.WORTHLESS for settlement in settlements
    )
    assert all(settlement.underlying_fill is None for settlement in settlements)
    assert all(settlement.settlement_value == ZERO for settlement in settlements)
    assert all(settlement.contracts == ZERO for settlement in settlements)
    assert hub.cash == Decimal("9950")
    assert hub.book("alpha").position(CALL_150).realized_pnl == Decimal("-200")
    assert hub.book("alpha").position(PUT_100).realized_pnl == Decimal("150")
    assert hub.equity == Decimal("9950")
    assert hub.total_pnl == Decimal("-50")
    assert lifecycle.retired_symbols == (CALL_150, PUT_100)
    assert lifecycle.settle_expired({"AAPL": "100"}) == ()
    assert hub.cash == Decimal("9950")


def test_long_itm_call_exercise_buys_exact_shares_at_strike() -> None:
    hub, clock, lifecycle = open_lifecycle(Decimal("20000"))
    lifecycle.apply_fill(
        "alpha", fill("long-call", Side.BUY, CALL_100, Decimal("1"), Decimal("200"))
    )

    clock.advance_to(EXPIRY)
    (exercise,) = lifecycle.settle_expired({"AAPL": "110"})

    assert exercise.kind is SettlementKind.EXERCISED
    assert exercise.underlying_fill is not None
    assert exercise.underlying_fill.side is Side.BUY
    assert exercise.underlying_fill.quantity == Decimal("100")
    assert exercise.underlying_fill.price == Decimal("100.000")
    book = hub.book("alpha")
    shares = book.position(AAPL)
    assert shares.quantity == Decimal("100")
    assert shares.average_cost == Decimal("100")
    assert book.position(CALL_100).is_flat
    assert book.position(CALL_100).realized_pnl == Decimal("-200")
    assert hub.cash == Decimal("9800")
    assert hub.realized_pnl == Decimal("-200")

    hub.mark(AAPL, "110")
    assert book.position(AAPL).unrealized_pnl == Decimal("1000")
    assert hub.equity == Decimal("20800")
    assert hub.total_pnl == Decimal("800")


def test_expired_contracts_reject_new_orders_and_retire_cleanly() -> None:
    hub, clock, lifecycle = open_lifecycle()
    lifecycle.apply_fill(
        "alpha", fill("long-put", Side.BUY, PUT_90, Decimal("1"), Decimal("100"))
    )
    clock.advance_to(EXPIRY)
    lifecycle.settle_expired({"AAPL": "120"})
    clock.advance_to(datetime(2024, 1, 20, 15, 0, tzinfo=UTC))
    before = hub.snapshot

    with pytest.raises(ExpiredContractError, match="retired"):
        lifecycle.apply_fill(
            "alpha", fill("late", Side.SELL, PUT_90, Decimal("1"), Decimal("50"))
        )
    with pytest.raises(ExpiredContractError, match="retired"):
        lifecycle.ensure_tradable(PUT_90)
    with pytest.raises(ExpiredContractError, match="expired"):
        lifecycle.apply_fill(
            "alpha", fill("fresh", Side.BUY, PUT_100, Decimal("1"), Decimal("50"))
        )
    with pytest.raises(ExpiredContractError, match="expired"):
        lifecycle.ensure_tradable(PUT_100)
    assert hub.snapshot == before
    assert lifecycle.ensure_tradable(AAPL) is None

    lifecycle.apply_fill(
        "alpha", fill("stock", Side.BUY, AAPL, Decimal("10"), Decimal("120"))
    )
    assert hub.book("alpha").position(AAPL).quantity == Decimal("10")
    assert hub.book("alpha").position(PUT_90).is_flat
    assert hub.book("alpha").position(PUT_90).realized_pnl == Decimal("-100")


def test_missing_spot_leaves_settlement_pending_and_state_unchanged() -> None:
    hub, clock, lifecycle = open_lifecycle()
    lifecycle.apply_fill(
        "alpha", fill("short-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"))
    )
    clock.advance_to(EXPIRY)
    before = hub.snapshot

    with pytest.raises(MissingUnderlyingPriceError, match="AAPL"):
        lifecycle.settle_expired({})
    with pytest.raises(MissingUnderlyingPriceError, match="AAPL"):
        lifecycle.settle_expired({"SPY": "500"})
    assert hub.snapshot == before
    assert not lifecycle.is_retired(PUT_90)
    assert lifecycle.open_contracts == (PUT_90,)

    (assignment,) = lifecycle.settle_expired({"AAPL": "85", "SPY": "500"})
    assert assignment.kind is SettlementKind.ASSIGNED
    assert hub.cash == Decimal("1100")


def test_dte_events_emit_once_per_threshold_in_deterministic_order() -> None:
    _, clock, lifecycle = open_lifecycle()
    lifecycle.apply_fill(
        "alpha", fill("long-put", Side.BUY, PUT_100, Decimal("1"), Decimal("300"))
    )
    clock.advance_to(datetime(2024, 1, 10, 15, 0, tzinfo=UTC))
    assert lifecycle.poll_dte_events() == ()

    clock.advance_to(datetime(2024, 1, 12, 15, 0, tzinfo=UTC))
    assert dte_records(lifecycle.poll_dte_events()) == [(PUT_100, 7, 7)]
    assert lifecycle.poll_dte_events() == ()

    clock.advance_to(datetime(2024, 1, 16, 15, 0, tzinfo=UTC))
    lifecycle.apply_fill(
        "alpha", fill("short-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"))
    )
    assert dte_records(lifecycle.poll_dte_events()) == [(PUT_90, 7, 3)]

    clock.advance_to(datetime(2024, 1, 18, 15, 0, tzinfo=UTC))
    events = lifecycle.poll_dte_events()
    assert dte_records(events) == [(PUT_90, 1, 1), (PUT_100, 1, 1)]
    assert lifecycle.poll_dte_events() == ()
    assert all(event.timestamp == clock.now() for event in events)


class HalfAssignment:
    """Stub policy that assigns exactly half of every short position."""

    def assigned_quantity(
        self, contract: OsiSymbol, spot: Decimal, short_quantity: Decimal
    ) -> Decimal:
        return short_quantity / Decimal("2")


class OverassignPolicy:
    """Stub policy that returns more than the short quantity."""

    def assigned_quantity(
        self, contract: OsiSymbol, spot: Decimal, short_quantity: Decimal
    ) -> Decimal:
        return short_quantity + 1


class NegativePolicy:
    """Stub policy that returns a negative quantity."""

    def assigned_quantity(
        self, contract: OsiSymbol, spot: Decimal, short_quantity: Decimal
    ) -> Decimal:
        return Decimal("-0.5")


class FloatPolicy:
    """Stub policy that returns a float instead of an exact decimal."""

    def assigned_quantity(
        self, contract: OsiSymbol, spot: Decimal, short_quantity: Decimal
    ) -> Decimal:
        return cast(Decimal, 0.5)


def test_partial_assignment_policy_settles_remainder_at_intrinsic() -> None:
    hub = PortfolioHub("paper-account")
    hub.create_book("alpha", Decimal("10000"))
    clock = SimClock(STAMP)
    lifecycle = OptionLifecycle(hub, clock, assignment_policy=HalfAssignment())
    lifecycle.apply_fill(
        "alpha", fill("short-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"))
    )

    clock.advance_to(EXPIRY)
    (assignment,) = lifecycle.settle_expired({"AAPL": "85"})

    assert assignment.kind is SettlementKind.ASSIGNED
    assert assignment.contracts == Decimal("0.5")
    assert assignment.settlement_value == Decimal("-250")
    assert assignment.underlying_fill is not None
    assert assignment.underlying_fill.quantity == Decimal("50")
    assert assignment.underlying_fill.price == Decimal("90.000")
    book = hub.book("alpha")
    assert book.position(AAPL).quantity == Decimal("50")
    assert book.position(AAPL).average_cost == Decimal("90")
    assert book.position(PUT_90).is_flat
    assert book.position(PUT_90).realized_pnl == Decimal("-150")
    assert hub.cash == Decimal("5350")
    assert hub.realized_pnl == Decimal("-150")

    hub.mark(AAPL, "85")
    assert hub.book("alpha").position(AAPL).unrealized_pnl == Decimal("-250")
    assert hub.equity == Decimal("9600")
    assert hub.total_pnl == Decimal("-400")


@pytest.mark.parametrize(
    "policy",
    [OverassignPolicy(), NegativePolicy()],
    ids=["overassign", "negative"],
)
def test_invalid_assignment_policies_leave_state_unchanged(
    policy: AssignmentPolicy,
) -> None:
    hub, clock, _ = open_lifecycle()
    lifecycle = OptionLifecycle(hub, clock, assignment_policy=policy)
    lifecycle.apply_fill(
        "alpha", fill("short-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"))
    )
    clock.advance_to(EXPIRY)
    before = hub.snapshot

    with pytest.raises(InvalidAssignmentError, match="assignment policy"):
        lifecycle.settle_expired({"AAPL": "85"})

    assert hub.snapshot == before
    assert not lifecycle.is_retired(PUT_90)


def test_float_assignment_policy_is_rejected_as_inexact() -> None:
    hub, clock, _ = open_lifecycle()
    lifecycle = OptionLifecycle(hub, clock, assignment_policy=FloatPolicy())
    lifecycle.apply_fill(
        "alpha", fill("short-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"))
    )
    clock.advance_to(EXPIRY)

    with pytest.raises(TypeError, match="exact Decimal"):
        lifecycle.settle_expired({"AAPL": "85"})


def test_settlement_covers_each_book_once_and_is_idempotent() -> None:
    hub = PortfolioHub("paper-account")
    hub.create_book("alpha", Decimal("10000"))
    hub.create_book("beta", Decimal("5000"))
    clock = SimClock(STAMP)
    lifecycle = OptionLifecycle(hub, clock)
    lifecycle.apply_fill(
        "alpha", fill("alpha-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"))
    )
    lifecycle.apply_fill(
        "beta", fill("beta-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"))
    )

    clock.advance_to(EXPIRY)
    settlements = lifecycle.settle_expired({"AAPL": "85"})

    assert [settlement.book_id for settlement in settlements] == ["alpha", "beta"]
    assert {
        settlement.underlying_fill.fill_id
        for settlement in settlements
        if settlement.underlying_fill is not None
    } == {f"settle:{PUT_90.value}:alpha", f"settle:{PUT_90.value}:beta"}
    assert hub.book("alpha").cash == Decimal("1100")
    assert hub.book("beta").cash == Decimal("-3900")
    assert hub.realized_pnl == Decimal("200")

    assert lifecycle.settle_expired({"AAPL": "85"}) == ()
    assert hub.cash == Decimal("-2800")
    assert lifecycle.retired_symbols == (PUT_90,)


def test_settlement_fee_applies_to_each_underlying_fill() -> None:
    hub = PortfolioHub("paper-account")
    hub.create_book("alpha", Decimal("10000"))
    clock = SimClock(STAMP)
    lifecycle = OptionLifecycle(hub, clock, settlement_fee=Decimal("1.50"))
    lifecycle.apply_fill(
        "alpha", fill("long-put", Side.BUY, PUT_100, Decimal("1"), Decimal("300"))
    )
    lifecycle.apply_fill(
        "alpha",
        fill("short-put", Side.SELL, PUT_90, Decimal("1"), Decimal("100"), sequence=1),
    )

    clock.advance_to(EXPIRY)
    settlements = lifecycle.settle_expired({"AAPL": "85"})

    assert all(
        settlement.underlying_fill is not None
        and settlement.underlying_fill.fee == Decimal("1.50")
        for settlement in settlements
    )
    assert hub.cash == Decimal("10797")
    assert hub.realized_pnl == Decimal("797")
    assert hub.equity == Decimal("10797")
    assert hub.total_pnl == Decimal("797")


def test_ensure_tradable_and_days_to_expiry_cover_the_expiry_boundary() -> None:
    _, clock, lifecycle = open_lifecycle()
    clock.advance_to(datetime(2024, 1, 18, 15, 0, tzinfo=UTC))
    assert lifecycle.days_to_expiry(PUT_100) == 1
    assert lifecycle.ensure_tradable(PUT_100) is None

    clock.advance_to(EXPIRY)
    assert lifecycle.days_to_expiry(PUT_100) == 0
    assert lifecycle.ensure_tradable(PUT_100) is None

    clock.advance_to(datetime(2024, 1, 20, 15, 0, tzinfo=UTC))
    assert lifecycle.days_to_expiry(PUT_100) == -1
    with pytest.raises(ExpiredContractError, match="expired"):
        lifecycle.ensure_tradable(PUT_100)
    with pytest.raises(ValueError, match="OSI"):
        lifecycle.days_to_expiry(AAPL)


def test_default_policy_is_pessimistic_full_assignment() -> None:
    _, _, lifecycle = open_lifecycle()
    assert isinstance(lifecycle.assignment_policy, PessimisticAssignment)
    assert lifecycle.multiplier == Decimal("100")
    assert lifecycle.dte_thresholds == (7, 1)


def test_lifecycle_rejects_invalid_configuration() -> None:
    hub = PortfolioHub("paper-account")
    clock = SimClock(STAMP)
    with pytest.raises(TypeError, match="PortfolioHub"):
        OptionLifecycle(cast(PortfolioHub, "hub"), clock)
    with pytest.raises(TypeError, match="now"):
        OptionLifecycle(hub, cast(Clock, "clock"))
    with pytest.raises(ValueError, match="multiplier"):
        OptionLifecycle(hub, clock, multiplier=0)
    with pytest.raises(ValueError, match="settlement_fee"):
        OptionLifecycle(hub, clock, settlement_fee=-1)
    with pytest.raises(ValueError, match="thresholds"):
        OptionLifecycle(hub, clock, dte_thresholds=(-1,))
    with pytest.raises(ValueError, match="thresholds"):
        OptionLifecycle(hub, clock, dte_thresholds=(1, True))
