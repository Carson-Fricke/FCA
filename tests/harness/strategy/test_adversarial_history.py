"""Adversarial checks that no strategy-reachable data surface leaks the future."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from harness.core.clock import SimClock
from harness.core.events import Event, EventBus
from harness.core.symbols import Symbol
from harness.core.types import QuoteEvent, TradeEvent
from harness.data.history import HistoricalView, LookaheadError, StrategyHistory
from harness.portfolio import PortfolioHub
from harness.strategy import PointInTimeData, Strategy, StrategyContext, bind_strategy

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")

FORBIDDEN_ATTRIBUTES = (
    "record",
    "record_many",
    "subscribe",
    "attach",
    "clock",
    "head",
    "capacity",
    "subscriptions",
)
PROBE_LABELS = (
    "rebind_record",
    "rebind_view",
    "future-read",
    "future-window",
    "future-aggregate",
    "future-bars",
)


class HistoryAdversary(Strategy):
    """Attempt every future-leak and poisoning surface on the data capability."""

    def __init__(self) -> None:
        self.context: StrategyContext | None = None
        self.blocked: list[str] = []
        self.escaped: list[str] = []
        self.reads: list[str] = []
        self.violations: list[str] = []

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._probe()

    def on_quote(self, event: QuoteEvent) -> None:
        self._probe(event)

    def on_trade(self, event: TradeEvent) -> None:
        self._probe(event)

    def _probe(self, event: QuoteEvent | TradeEvent | None = None) -> None:
        context = self.context
        assert context is not None
        history = context.data
        head = context.clock.now()
        if history is None:
            self.escaped.append("capability:absent")
            return
        if not isinstance(history, StrategyHistory):
            self.escaped.append("capability:unwrapped")
            return
        for name in FORBIDDEN_ATTRIBUTES:
            if hasattr(history, name):
                self.escaped.append(f"attribute:{name}")
        self._attempt(
            "rebind_record",
            lambda: setattr(history, "record", lambda candidate: None),  # noqa: B010
            AttributeError,
        )
        self._attempt(
            "rebind_view",
            lambda: setattr(  # noqa: B010
                history, "_view", HistoricalView(SimClock(head))
            ),
            AttributeError,
        )
        future_stamp = head + timedelta(days=1)
        self._attempt(
            "future-read",
            lambda: history.read(
                future_stamp,  # ty: ignore[invalid-argument-type]
                AAPL,  # ty: ignore[invalid-argument-type]
            ),
            TypeError,
        )
        self._attempt(
            "future-window",
            lambda: history.window(AAPL, STAMP, head + timedelta(seconds=1)),
            LookaheadError,
        )
        self._attempt(
            "future-aggregate",
            lambda: history.aggregate(AAPL, STAMP, head + timedelta(seconds=1)),
            LookaheadError,
        )
        self._attempt(
            "future-bars",
            lambda: history.bars(AAPL, "1min", STAMP, head + timedelta(seconds=1)),
            LookaheadError,
        )
        if event is not None:
            self._observe(history, event, head)

    def _observe(
        self,
        history: StrategyHistory,
        event: QuoteEvent | TradeEvent,
        head: datetime,
    ) -> None:
        read = history.read(event.symbol)
        if not isinstance(read, QuoteEvent | TradeEvent):
            self.violations.append(f"{event.event_id}:missing-read")
            return
        self.reads.append(read.event_id)
        if read.timestamp > head:
            self.violations.append(f"{event.event_id}:future:{read.event_id}")
        field = "bid_price"
        try:
            setattr(read, field, Decimal("1"))
        except FrozenInstanceError:
            self.blocked.append(f"freeze:{event.event_id}")
        else:
            self.escaped.append(f"freeze:{event.event_id}")

    def _attempt(
        self,
        label: str,
        action: Callable[[], object],
        expected: type[BaseException],
    ) -> None:
        try:
            action()
        except expected:
            self.blocked.append(label)
        else:
            self.escaped.append(label)


def _quote(event_id: str, stamp: datetime, sequence: int) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        stamp,
        sequence,
        AAPL,
        Decimal("99"),
        Decimal("1"),
        Decimal("101"),
        Decimal("1"),
    )


def _context(clock: SimClock) -> tuple[EventBus, StrategyContext, HistoricalView]:
    view = HistoricalView(clock, capacity=16)
    hub = PortfolioHub("account")
    hub.create_book("strategy", "1000")
    bus = EventBus(clock)
    view.subscribe(AAPL, Event)
    view.attach(bus)
    context = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        data=view,
        universe=(AAPL,),
        subscriptions=(QuoteEvent, TradeEvent),
        book_id="strategy",
        seed=1729,
    )
    return bus, context, view


def test_adversarial_strategy_cannot_leak_the_future_or_poison_history() -> None:
    clock = SimClock(STAMP)
    bus, context, view = _context(clock)
    binding = bind_strategy(HistoryAdversary, {}, context)
    binding.start()
    bus.publish(_quote("q0", STAMP, 0))
    bus.drain()
    clock.advance_by(timedelta(seconds=1))
    bus.publish(_quote("q1", STAMP + timedelta(seconds=1), 1))
    bus.drain()
    binding.stop()

    adversary = binding.strategy
    assert isinstance(adversary, HistoryAdversary)
    assert adversary.escaped == []
    assert adversary.violations == []
    assert adversary.blocked == (
        list(PROBE_LABELS)
        + list(PROBE_LABELS)
        + ["freeze:q0"]
        + list(PROBE_LABELS)
        + ["freeze:q1"]
    )
    assert adversary.reads == ["q0", "q1"]
    assert view.retained(AAPL) == 2


def test_context_wraps_the_engine_view_into_the_opaque_capability() -> None:
    clock = SimClock(STAMP)
    _, context, view = _context(clock)

    capability = context.data
    assert isinstance(capability, StrategyHistory)
    assert capability is not view
    assert context.history is context.data
    assert capability.read(AAPL) is None
    assert not hasattr(capability, "record")


def test_capability_satisfies_the_point_in_time_data_protocol() -> None:
    clock = SimClock(STAMP)
    _, context, _ = _context(clock)

    capability = context.data
    assert isinstance(capability, PointInTimeData)
