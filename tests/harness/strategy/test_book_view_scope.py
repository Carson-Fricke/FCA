"""Book-view scope and reserved-scheduler refusal at the strategy boundary."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types import (
    AccountSnapshot,
    Book,
    Event,
    ExecutionReport,
    ExecutionType,
    Fill,
    OrderStatus,
    Position,
    QuoteEvent,
    Side,
    TradeEvent,
)
from harness.portfolio import PortfolioHub, PortfolioSnapshot, UnknownBookError
from harness.strategy import (
    EventScheduler,
    Strategy,
    StrategyBinding,
    StrategyContext,
    Subscription,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")
SPY = Symbol("SPY")


def _fill(fill_id: str, symbol: Symbol = AAPL) -> Fill:
    return Fill(
        f"fill-{fill_id}",
        STAMP,
        1,
        fill_id,
        f"order-{fill_id}",
        symbol,
        Side.BUY,
        Decimal("2"),
        Decimal("100"),
    )


def _quote(event_id: str, symbol: Symbol = AAPL) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP,
        1,
        symbol,
        Decimal("99"),
        Decimal("1"),
        Decimal("101"),
        Decimal("1"),
    )


def _two_book_contexts() -> tuple[
    EventBus,
    StrategyContext,
    StrategyContext,
    PortfolioHub,
]:
    clock = SimClock(STAMP)
    bus = EventBus(clock)
    hub = PortfolioHub("account")
    hub.create_book("alpha", "1000")
    hub.create_book("beta", "1000")
    alpha = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        book_id="alpha",
        universe=(AAPL,),
        subscriptions=(Subscription(QuoteEvent, (AAPL,)),),
        seed=7,
    )
    beta = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        book_id="beta",
        universe=(SPY,),
        subscriptions=(Subscription(QuoteEvent, (SPY,)),),
        seed=13,
    )
    return bus, alpha, beta, hub


def _reserved_events() -> tuple[Event, ...]:
    return (
        QuoteEvent(
            "quote-1",
            STAMP,
            1,
            AAPL,
            Decimal("99"),
            Decimal("1"),
            Decimal("101"),
            Decimal("1"),
        ),
        TradeEvent("trade-1", STAMP, 1, AAPL, Decimal("100"), Decimal("1")),
        ExecutionReport(
            "report-1",
            STAMP,
            1,
            "report-1",
            "order-1",
            OrderStatus.FILLED,
            ExecutionType.FILL,
            Decimal("2"),
            Decimal("2"),
            Decimal("0"),
            Decimal("100"),
            Decimal("2"),
            Decimal("100"),
        ),
        Fill(
            "fill-1",
            STAMP,
            1,
            "fill-1",
            "order-1",
            AAPL,
            Side.BUY,
            Decimal("2"),
            Decimal("100"),
        ),
        Position("position-1", STAMP, 1, "account", AAPL, Decimal("2"), Decimal("100")),
        Book("book-1", STAMP, 1, AAPL),
        AccountSnapshot(
            "account-1",
            STAMP,
            1,
            "account",
            Decimal("800"),
            Decimal("1000"),
            Decimal("800"),
        ),
    )


def test_books_view_resolves_only_the_own_book() -> None:
    _, alpha, _, _ = _two_book_contexts()

    assert alpha.books.book_id == "alpha"
    assert alpha.books.book_ids == ("alpha",)
    assert alpha.books.book("alpha").book_id == "alpha"
    with pytest.raises(UnknownBookError):
        alpha.books.book("beta")


def test_scoped_portfolio_snapshot_hides_sibling_books() -> None:
    _, alpha, _, hub = _two_book_contexts()
    hub.apply_fill("beta", _fill("fill-beta", SPY))

    snapshot = alpha.portfolio

    assert isinstance(snapshot, PortfolioSnapshot)
    assert tuple(book.book_id for book in snapshot.books) == ("alpha",)
    with pytest.raises(UnknownBookError):
        snapshot.book("beta")
    with pytest.raises(UnknownBookError):
        alpha.books.book("beta")
    assert alpha.books.book_ids == ("alpha",)
    account_snapshot = alpha.account.snapshot
    assert isinstance(account_snapshot, PortfolioSnapshot)
    assert account_snapshot.books == snapshot.books


def test_scoped_snapshot_preserves_shared_account_aggregates() -> None:
    _, alpha, _, hub = _two_book_contexts()
    hub.apply_fill("alpha", _fill("fill-alpha"))

    snapshot = alpha.portfolio

    assert snapshot.cash == hub.snapshot.cash
    assert snapshot.fill_count == hub.snapshot.fill_count
    assert snapshot.account_id == hub.account_id
    assert alpha.books.current.cash == Decimal("800")
    assert alpha.books.current.fill_count == 1
    assert alpha.books.position(AAPL).quantity == Decimal("2")
    assert snapshot.position(AAPL).quantity == Decimal("2")


def test_account_view_fallback_is_scoped_and_event_wins() -> None:
    _, alpha, _, _ = _two_book_contexts()
    fallback = alpha.account.snapshot
    assert isinstance(fallback, PortfolioSnapshot)
    assert tuple(book.book_id for book in fallback.books) == ("alpha",)

    account_event = AccountSnapshot(
        "account-1",
        STAMP,
        1,
        "account",
        Decimal("1800"),
        Decimal("2000"),
        Decimal("1800"),
    )
    alpha.account.update(account_event)

    assert alpha.account.snapshot is account_event
    assert alpha.account.cash == Decimal("1800")
    assert alpha.account.buying_power == Decimal("1800")
    assert alpha.account.account_id == "account"


class BookProbeStrategy(Strategy):
    def __init__(self) -> None:
        self.context: StrategyContext
        self.probe_error: str | None = None
        self.view_book_ids: tuple[str, ...] = ()
        self.snapshot_book_ids: tuple[str, ...] = ()
        self.account_book_ids: tuple[str, ...] = ()

    def on_start(self, context: StrategyContext) -> None:
        self.context = context

    def on_quote(self, event: QuoteEvent) -> None:
        try:
            self.context.books.book("beta")
        except UnknownBookError:
            self.probe_error = "UnknownBookError"
        self.view_book_ids = self.context.books.book_ids
        self.snapshot_book_ids = tuple(
            book.book_id for book in self.context.portfolio.books
        )
        account_snapshot = self.context.account.snapshot
        if isinstance(account_snapshot, PortfolioSnapshot):
            self.account_book_ids = tuple(
                book.book_id for book in account_snapshot.books
            )


def test_cross_book_probing_strategy_sees_nothing_of_siblings() -> None:
    bus, alpha, beta, hub = _two_book_contexts()
    hub.apply_fill("beta", _fill("fill-beta", SPY))

    probe = BookProbeStrategy()
    binding = StrategyBinding(probe, alpha)
    binding.start()
    bus.publish(_quote("quote-aapl"))
    bus.drain()
    binding.stop()

    assert probe.probe_error == "UnknownBookError"
    assert probe.view_book_ids == ("alpha",)
    assert probe.snapshot_book_ids == ("alpha",)
    assert probe.account_book_ids == ("alpha",)
    assert beta.books.current.fill_count == 1
    assert beta.books.current.cash == Decimal("800")
    assert alpha.books.current.fill_count == 0
    assert alpha.books.current.cash == Decimal("1000")


def test_book_snapshots_are_frozen_and_independent() -> None:
    _, alpha, _, hub = _two_book_contexts()
    stale = alpha.books.snapshot
    own = alpha.books.current

    with pytest.raises(FrozenInstanceError):
        setattr(own, "cash", Decimal("0"))  # noqa: B010
    with pytest.raises(FrozenInstanceError):
        setattr(stale, "cash", Decimal("0"))  # noqa: B010
    with pytest.raises(FrozenInstanceError):
        setattr(stale.books[0], "cash", Decimal("0"))  # noqa: B010

    hub.apply_fill("alpha", _fill("fill-alpha"))

    assert own.fill_count == 0
    assert own.cash == Decimal("1000")
    assert stale.books[0].fill_count == 0
    assert alpha.books.current.fill_count == 1
    assert alpha.books.current.cash == Decimal("800")


def test_scheduler_refuses_reserved_event_classes() -> None:
    bus, context, _, _ = _two_book_contexts()
    assert isinstance(context.scheduler, EventScheduler)

    for event in _reserved_events():
        with pytest.raises(TypeError, match="reserved for the feed"):
            context.schedule(event)
        with pytest.raises(TypeError, match="reserved for the feed"):
            context.scheduler.schedule(event)

    scheduler = EventScheduler(bus)
    with pytest.raises(TypeError, match="reserved for the feed"):
        scheduler.schedule(_reserved_events()[0])
