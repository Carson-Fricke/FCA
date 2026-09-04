from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from harness.core.symbols import OsiSymbol, Symbol
from harness.core.types.accounting import Fill
from harness.core.types.orders import Side
from harness.portfolio import (
    DuplicateFillError,
    PortfolioError,
    PositionMarker,
    UnknownPositionError,
    VirtualBook,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")
OPTION = OsiSymbol("AAPL240119P00100000")
ZERO = Decimal("0")


def fill(
    fill_id: str,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    *,
    fee: Decimal = ZERO,
    symbol: Symbol = AAPL,
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


def test_average_cost_partial_close_reversal_and_fees_are_exact() -> None:
    book = VirtualBook("alpha", Decimal("10000"))

    first = book.apply_fill(
        fill(
            "buy-1",
            Side.BUY,
            Decimal("10"),
            Decimal("100"),
            fee=Decimal("1"),
        )
    )
    assert first.quantity == Decimal("10")
    assert first.average_cost == Decimal("100")
    assert first.realized_pnl == Decimal("-1")
    assert first.unrealized_pnl is None
    assert book.cash == Decimal("8999")
    assert book.equity is None

    book.apply_fill(
        fill(
            "buy-2",
            Side.BUY,
            Decimal("5"),
            Decimal("110"),
            fee=Decimal("0.5"),
            sequence=1,
        )
    )
    partial = book.apply_fill(
        fill(
            "sell-part",
            Side.SELL,
            Decimal("6"),
            Decimal("120"),
            fee=Decimal("0.25"),
            sequence=2,
        )
    )
    assert partial.quantity == Decimal("9")
    assert partial.cost_basis == Decimal("930")
    assert partial.average_cost == Decimal("103.3333333333333333333333333")
    assert partial.realized_pnl == Decimal("98.25")

    reversed_position = book.apply_fill(
        fill(
            "sell-through",
            Side.SELL,
            Decimal("12"),
            Decimal("90"),
            fee=Decimal("0.75"),
            sequence=3,
        )
    )
    assert reversed_position.quantity == Decimal("-3")
    assert reversed_position.cost_basis == Decimal("-270")
    assert reversed_position.average_cost == Decimal("90")
    assert reversed_position.realized_pnl == Decimal("-22.50")
    assert book.cash == Decimal("10247.50")

    marker = book.mark(AAPL, Decimal("85"))
    marked = book.position(AAPL)
    assert marker == PositionMarker(AAPL, Decimal("85"))
    assert marked.mark_price == Decimal("85")
    assert marked.unrealized_pnl == Decimal("15")
    assert marked.total_pnl == Decimal("-7.50")
    assert book.equity == Decimal("9992.50")
    assert book.total_pnl == Decimal("-7.50")
    unrealized_pnl = book.unrealized_pnl
    assert unrealized_pnl is not None
    assert book.total_pnl == book.realized_pnl + unrealized_pnl


def test_flat_position_retains_realized_pnl_and_needs_no_marker() -> None:
    book = VirtualBook("round-trip", "1000")
    book.apply_fill(fill("buy", Side.BUY, Decimal("2"), Decimal("10")))
    book.apply_fill(
        fill(
            "buy-recurring-average",
            Side.BUY,
            Decimal("1"),
            Decimal("11"),
            sequence=1,
        )
    )
    book.apply_fill(
        fill(
            "partial-close",
            Side.SELL,
            Decimal("1"),
            Decimal("12"),
            sequence=2,
        )
    )
    closed = book.apply_fill(
        fill(
            "sell",
            Side.SELL,
            Decimal("2"),
            Decimal("10"),
            fee=Decimal("0.1"),
            sequence=3,
        )
    )

    assert closed.is_flat
    assert closed.average_cost == ZERO
    assert closed.cost_basis == ZERO
    assert closed.realized_pnl == Decimal("0.9")
    assert closed.market_value == ZERO
    assert closed.unrealized_pnl == ZERO
    assert book.cash == Decimal("1000.9")
    assert book.equity == Decimal("1000.9")
    assert book.total_pnl == Decimal("0.9")


def test_duplicate_fill_and_invalid_marker_leave_book_unchanged() -> None:
    book = VirtualBook("alpha", "100")
    applied = fill("one", Side.BUY, Decimal("1"), Decimal("10"))
    book.apply_fill(applied)
    before = book.snapshot

    with pytest.raises(DuplicateFillError):
        book.apply_fill(applied)
    with pytest.raises(TypeError, match="exact Decimal"):
        book.mark(AAPL, cast(Decimal, 10.5))
    with pytest.raises(ValueError, match="positive"):
        book.mark(AAPL, ZERO)

    assert book.snapshot == before


def test_retire_position_finalizes_expired_contracts_exactly() -> None:
    book = VirtualBook("expiry", Decimal("1000"))
    book.apply_fill(
        fill("buy-option", Side.BUY, Decimal("1"), Decimal("200"), symbol=OPTION)
    )
    worthless = book.retire_position(OPTION)

    assert worthless.is_flat
    assert worthless.cost_basis == ZERO
    assert worthless.realized_pnl == Decimal("-200")
    assert worthless.market_value == ZERO
    assert book.cash == Decimal("800")
    assert book.snapshot.equity == Decimal("800")
    assert book.snapshot.total_pnl == Decimal("-200")

    assigned = VirtualBook("assignment", Decimal("1000"))
    assigned.apply_fill(
        fill(
            "sell-option",
            Side.SELL,
            Decimal("1"),
            Decimal("100"),
            symbol=OPTION,
        )
    )
    settled = assigned.retire_position(OPTION, settlement_value=Decimal("-250"))

    assert settled.realized_pnl == Decimal("-150")
    assert assigned.cash == Decimal("850")

    with pytest.raises(PortfolioError, match="already flat"):
        book.retire_position(OPTION)
    with pytest.raises(UnknownPositionError):
        book.retire_position(AAPL)


def generated_fill_sequences(count: int) -> tuple[tuple[Fill, ...], ...]:
    sequences: list[tuple[Fill, ...]] = []
    for case in range(count):
        fills: list[Fill] = []
        for step in range(5):
            side = Side.BUY if ((case >> step) + step) % 2 == 0 else Side.SELL
            quantity = Decimal((case * (step + 3) + step) % 7 + 1)
            price = Decimal(9000 + (case * 37 + step * 101) % 3001).scaleb(-2)
            fee = Decimal((case + step * 3) % 7 - 2).scaleb(-2)
            sequence = case * 5 + step
            fills.append(
                fill(
                    f"{case}-{step}",
                    side,
                    quantity,
                    price,
                    fee=fee,
                    sequence=sequence,
                )
            )
        sequences.append(tuple(fills))
    return tuple(sequences)


def test_ten_thousand_generated_fill_sequences_conserve_cash_and_position() -> None:
    initial_cash = Decimal("100000")
    for case, fills in enumerate(generated_fill_sequences(10_000)):
        book = VirtualBook(f"generated-{case}", initial_cash)
        expected_cash = initial_cash
        expected_quantity = ZERO

        for generated_fill in fills:
            signed_quantity = (
                generated_fill.quantity
                if generated_fill.side is Side.BUY
                else -generated_fill.quantity
            )
            expected_cash -= signed_quantity * generated_fill.price + generated_fill.fee
            expected_quantity += signed_quantity
            book.apply_fill(generated_fill)

        mark = Decimal(9500 + case % 2001).scaleb(-2)
        book.mark(AAPL, mark)
        position = book.position(AAPL)
        snapshot = book.snapshot

        assert book.cash == expected_cash
        assert position.quantity == expected_quantity
        assert snapshot.market_value == expected_quantity * mark
        assert snapshot.equity == expected_cash + expected_quantity * mark
        assert snapshot.unrealized_pnl is not None
        assert snapshot.total_pnl == snapshot.realized_pnl + snapshot.unrealized_pnl
