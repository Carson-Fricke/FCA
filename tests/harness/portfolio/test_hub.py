from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types.accounting import Fill
from harness.core.types.orders import Side
from harness.portfolio import (
    DuplicateBookError,
    DuplicateFillError,
    PortfolioHub,
    UnknownBookError,
    VirtualBook,
)

AAPL = Symbol("AAPL")
STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)


def fill(
    fill_id: str,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    *,
    sequence: int = 0,
) -> Fill:
    return Fill(
        event_id=f"event-{fill_id}",
        timestamp=STAMP + timedelta(microseconds=sequence),
        sequence=sequence,
        fill_id=fill_id,
        order_id=f"order-{fill_id}",
        symbol=AAPL,
        side=side,
        quantity=quantity,
        price=price,
    )


def test_two_strategies_share_symbol_with_independent_pnl_and_global_totals() -> None:
    hub = PortfolioHub("paper-account")
    hub.create_book("alpha", Decimal("10000"))
    hub.create_book("beta", Decimal("5000"))

    hub.apply_fill("alpha", fill("alpha-buy", Side.BUY, Decimal("10"), Decimal("100")))
    hub.apply_fill(
        "alpha",
        fill(
            "alpha-sell",
            Side.SELL,
            Decimal("4"),
            Decimal("110"),
            sequence=1,
        ),
    )
    hub.apply_fill(
        "beta",
        fill(
            "beta-sell",
            Side.SELL,
            Decimal("5"),
            Decimal("105"),
            sequence=2,
        ),
    )
    hub.apply_fill(
        "beta",
        fill(
            "beta-buy",
            Side.BUY,
            Decimal("2"),
            Decimal("95"),
            sequence=3,
        ),
    )

    assert hub.unrealized_pnl is None
    hub.mark(AAPL, Decimal("108"))
    snapshot = hub.snapshot
    alpha = snapshot.book("alpha")
    beta = snapshot.book("beta")
    aggregate = snapshot.position(AAPL)

    assert alpha.position(AAPL).quantity == Decimal("6")
    assert alpha.realized_pnl == Decimal("40")
    assert alpha.unrealized_pnl == Decimal("48")
    assert alpha.total_pnl == Decimal("88")
    assert beta.position(AAPL).quantity == Decimal("-3")
    assert beta.realized_pnl == Decimal("20")
    assert beta.unrealized_pnl == Decimal("-9")
    assert beta.total_pnl == Decimal("11")

    assert aggregate.quantity == Decimal("3")
    assert aggregate.long_quantity == Decimal("6")
    assert aggregate.short_quantity == Decimal("3")
    assert aggregate.gross_quantity == Decimal("9")
    assert aggregate.average_cost is None
    assert aggregate.realized_pnl == Decimal("60")
    assert aggregate.unrealized_pnl == Decimal("39")
    assert aggregate.total_pnl == Decimal("99")
    assert snapshot.initial_cash == Decimal("15000")
    assert snapshot.cash == Decimal("14775")
    assert snapshot.market_value == Decimal("324")
    assert snapshot.equity == Decimal("15099")
    assert snapshot.total_pnl == Decimal("99")
    unrealized_pnl = snapshot.unrealized_pnl
    assert unrealized_pnl is not None
    assert snapshot.total_pnl == snapshot.realized_pnl + unrealized_pnl


def test_hub_broadcasts_markers_to_existing_and_future_books() -> None:
    hub = PortfolioHub("paper-account", [VirtualBook("first", "100")])
    marker = hub.mark(AAPL, Decimal("101.25"))
    second = hub.create_book("second", "200")

    assert hub.book("first").marker(AAPL) == marker
    assert second.marker(AAPL) == marker


def test_hub_rejects_duplicate_books_and_cross_book_fill_routes() -> None:
    hub = PortfolioHub("paper-account")
    hub.create_book("alpha", "100")
    hub.create_book("beta", "100")
    routed_fill = fill("routed", Side.BUY, Decimal("1"), Decimal("10"))
    hub.apply_fill("alpha", routed_fill)
    before = hub.snapshot

    with pytest.raises(DuplicateFillError, match="alpha"):
        hub.apply_fill("beta", routed_fill)
    with pytest.raises(DuplicateBookError):
        hub.register_book(VirtualBook("alpha", "100"))
    with pytest.raises(UnknownBookError):
        hub.apply_fill(
            "missing",
            fill("not-routed", Side.SELL, Decimal("1"), Decimal("10")),
        )

    assert hub.snapshot == before
