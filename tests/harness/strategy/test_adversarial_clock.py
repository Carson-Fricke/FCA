"""Adversarial checks that no strategy-reachable surface can advance the clock."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.clock import ReadonlyClock, SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types import QuoteEvent
from harness.portfolio import PortfolioHub
from harness.strategy import Strategy, StrategyContext, bind_strategy

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")

MUTATION_ATTRIBUTES = ("advance_to", "advance_by", "head")
BLOCKED_LABELS = (
    "advance_to",
    "advance_by",
    "set_head",
    "rebind_now",
    "rebind_internal_clock",
    "delete_internal_clock",
    "rebind_context_clock",
)


class ClockAdversary(Strategy):
    """Attempt every clock-mutation surface reachable from strategy code."""

    def __init__(self) -> None:
        self.context: StrategyContext | None = None
        self.blocked: list[str] = []
        self.escaped: list[str] = []
        self.observed: list[datetime] = []

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self.observed.append(context.clock.now())
        self._probe(context)

    def on_quote(self, event: QuoteEvent) -> None:
        assert self.context is not None
        self.observed.append(self.context.clock.now())
        self._probe(self.context)

    def _probe(self, context: StrategyContext) -> None:
        clock = context.clock
        target = clock.now() + timedelta(days=1)
        for name in MUTATION_ATTRIBUTES:
            if hasattr(clock, name):
                self.escaped.append(f"attribute:{name}")
        self._attempt(
            "advance_to",
            lambda: getattr(clock, "advance_to")(target),  # noqa: B009 - dynamic probe
        )
        self._attempt(
            "advance_by",
            lambda: getattr(clock, "advance_by")(  # noqa: B009 - dynamic probe
                timedelta(days=1)
            ),
        )
        self._attempt(
            "set_head",
            lambda: setattr(clock, "head", target),  # noqa: B010
        )
        self._attempt(
            "rebind_now",
            lambda: setattr(clock, "now", lambda: target),  # noqa: B010
        )
        self._attempt(
            "rebind_internal_clock",
            lambda: setattr(clock, "_clock", SimClock(target)),  # noqa: B010
        )
        self._attempt("delete_internal_clock", lambda: delattr(clock, "_clock"))
        self._attempt(
            "rebind_context_clock",
            lambda: setattr(context, "clock", clock),  # noqa: B010
        )

    def _attempt(self, label: str, action: Callable[[], object]) -> None:
        try:
            action()
        except AttributeError:
            self.blocked.append(label)
        else:
            self.escaped.append(label)


def _context(clock: SimClock) -> tuple[EventBus, StrategyContext]:
    hub = PortfolioHub("account")
    hub.create_book("strategy", "1000")
    bus = EventBus(clock)
    context = StrategyContext(
        ReadonlyClock(clock),
        portfolio=hub,
        bus=bus,
        universe=(AAPL,),
        subscriptions=(QuoteEvent,),
        book_id="strategy",
        seed=1729,
    )
    return bus, context


def _quote(event_id: str, stamp: datetime) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        stamp,
        1,
        AAPL,
        Decimal("99"),
        Decimal("1"),
        Decimal("101"),
        Decimal("1"),
    )


def test_adversarial_strategy_cannot_advance_the_engine_clock() -> None:
    clock = SimClock(STAMP)
    bus, context = _context(clock)
    binding = bind_strategy(ClockAdversary, {}, context)
    binding.start()
    bus.publish(_quote("q0", STAMP))
    bus.drain()
    clock.advance_by(timedelta(seconds=1))
    bus.publish(_quote("q1", STAMP + timedelta(seconds=1)))
    bus.drain()
    binding.stop()

    adversary = binding.strategy
    assert isinstance(adversary, ClockAdversary)
    assert adversary.escaped == []
    assert tuple(adversary.blocked) == BLOCKED_LABELS * 3
    assert adversary.observed == [
        STAMP,
        STAMP,
        STAMP + timedelta(seconds=1),
    ]
    assert clock.now() == STAMP + timedelta(seconds=1)


def test_strategy_context_wraps_a_raw_clock_into_the_read_only_capability() -> None:
    clock = SimClock(STAMP)
    _, context = _context(clock)

    view = context.clock
    assert isinstance(view, ReadonlyClock)
    assert view.now() == STAMP
    assert not hasattr(view, "advance_to")
    assert not hasattr(view, "advance_by")
    assert not hasattr(view, "head")
    with pytest.raises(AttributeError):
        getattr(view, "advance_to")(STAMP + timedelta(days=1))  # noqa: B009
    with pytest.raises(AttributeError):
        setattr(context, "clock", clock)  # noqa: B010
    assert clock.now() == STAMP
