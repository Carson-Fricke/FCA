"""Consolidated adversarial look-ahead detector suite.

Every attack family from the T190 structural audit (§4, D7) is built here as
a strategy that attempts the attack inside its callbacks while a
kernel-shaped harness drives it through several dispatched events, including
one window in which a genuinely future event sits queued but undispatched.
The suite proves the structural claims of the strategy boundary stay true as
the code evolves:

* (a) clock advance is unrepresentable on the read-only capability;
* (b) future data is unrepresentable: timestamped reads fail by signature
  (documented with paired ``# ty: ignore[invalid-argument-type]`` markers so
  ``ty`` enforces the same claim) and future window, aggregate, and bar
  bounds raise :class:`LookaheadError` as defense-in-depth;
* (c) history-poisoning surfaces (``record``/``subscribe``/``attach``) are
  absent by construction;
* (d) fabricated market data is refused for each of the seven reserved event
  classes while strategy-owned events remain schedulable;
* (e) the one-way venue command capability carries no introspection surface
  such as ``remaining_steps`` or ``decisions_for``;
* (f) sibling books are unrepresentable through the scoped views.

Boundary note (audit finding R1): "structural" here means *unreachable
through the sanctioned capability surface*, not interpreter-level sandboxing.
Deliberate private-slot or dynamic-attribute escapes are out of contract and
are the territory of the CI sandbox lint sweep (defense-in-depth), so this
suite probes only the sanctioned ``context.*`` surfaces.  No assertion below
depends on an engine runtime guard: every refusal comes from the capability's
own shape or from its internal :class:`LookaheadError`.

The focused suites ``test_adversarial_clock.py`` (clock capability) and
``test_adversarial_history.py`` (data capability) remain authoritative for
their single surfaces; this file adds the cross-cutting matrix in which every
capability is present at once, so a regression in any single boundary cannot
hide behind a stripped-down harness.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from harness.core.clock import SimClock
from harness.core.events import Event, EventBus
from harness.core.symbols import Symbol
from harness.core.types import (
    AccountSnapshot,
    Book,
    ExecutionReport,
    ExecutionType,
    Fill,
    OrderStatus,
    Position,
    QuoteEvent,
    Side,
    TradeEvent,
)
from harness.core.types.orders import OrderRequest
from harness.data.history import HistoricalView, LookaheadError, StrategyHistory
from harness.portfolio import PortfolioHub, PortfolioSnapshot, UnknownBookError
from harness.runtime.backtest.strategies import StrategyVenue
from harness.strategy import (
    Strategy,
    StrategyBinding,
    StrategyContext,
    bind_strategy,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
AAPL = Symbol("AAPL")
SPY = Symbol("SPY")

PROBE_ROUNDS = 4  # on_start plus one probe per dispatched quote

RESERVED_EVENT_CLASSES = (
    QuoteEvent,
    TradeEvent,
    ExecutionReport,
    Fill,
    Position,
    Book,
    AccountSnapshot,
)

CLOCK_ABSENT_ATTRIBUTES = ("advance_to", "advance_by", "head")
CLOCK_PROBE_LABELS = (
    "advance_to",
    "advance_by",
    "write_head",
    "rebind_now",
    "rebind_context_clock",
)

TIME_TRAVEL_LABELS = (
    "typed-future-read",
    "future-window",
    "future-aggregate",
    "future-bars",
)

POISONER_ABSENT_ATTRIBUTES = (
    "record",
    "record_many",
    "subscribe",
    "attach",
    "capacity",
    "subscriptions",
    "head",
    "clock",
)
POISONER_LABELS = ("write_record", "write_view", "write_clock", "delete_record")

FABRICATOR_LABELS = tuple(
    f"{surface}:{event_class.__name__}"
    for event_class in RESERVED_EVENT_CLASSES
    for surface in ("schedule", "scheduler")
)

VENUE_INTROSPECTION_ATTRIBUTES = (
    "state",
    "decisions_for",
    "active_orders",
    "latest_market",
    "calls",
    "remaining_steps",
)
VENUE_COMMANDS = ("submit", "cancel", "replace", "request_position_snapshot")

SNOOPING_LABELS = (
    "sibling-book",
    "portfolio-sibling-book",
    "account-sibling-book",
)


class _AttackRecord:
    """Shared recorder separating blocked refusals from escapes."""

    def __init__(self) -> None:
        self.blocked: list[str] = []
        self.escaped: list[str] = []
        self.violations: list[str] = []
        self.allowed: list[str] = []
        self.messages: dict[str, str] = {}

    def _attempt(
        self,
        label: str,
        action: Callable[[], object],
        expected: type[BaseException],
    ) -> None:
        try:
            action()
        except expected as error:
            self.blocked.append(label)
            self.messages[label] = str(error)
        except Exception as error:  # unexpected failure counts as an escape
            self.escaped.append(f"{label}:{type(error).__name__}")
        else:
            self.escaped.append(label)

    def _permit(self, label: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as error:  # a sanctioned surface must not raise
            self.escaped.append(f"{label}:{type(error).__name__}")
        else:
            self.allowed.append(label)

    def _absent(self, label: str, surface: object, attribute: str) -> None:
        if hasattr(surface, attribute):
            self.escaped.append(f"{label}:{attribute}")


class _RecordingVenue:
    """Minimal concrete venue that records the fire-and-forget commands."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit(self, order: OrderRequest) -> None:
        self.calls.append(f"submit:{order.order_id}")

    def cancel(self, order_id: str) -> None:
        self.calls.append(f"cancel:{order_id}")

    def replace(self, order_id: str, replacement: OrderRequest) -> None:
        self.calls.append(f"replace:{order_id}")

    def request_position_snapshot(self, account_id: str) -> None:
        self.calls.append(f"snapshot:{account_id}")


class _Run(NamedTuple):
    """One adversarial harness run and its kernel-owned pieces."""

    binding: StrategyBinding
    bus: EventBus
    clock: SimClock
    view: HistoricalView
    recording_venue: _RecordingVenue
    hub: PortfolioHub


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


def _fill(fill_id: str, symbol: Symbol) -> Fill:
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


def _reserved_events() -> tuple[Event, ...]:
    """Build one fabricated instance of every reserved event class."""
    return (
        QuoteEvent(
            "fabricated-quote",
            STAMP,
            1,
            AAPL,
            Decimal("99"),
            Decimal("1"),
            Decimal("101"),
            Decimal("1"),
        ),
        TradeEvent("fabricated-trade", STAMP, 1, AAPL, Decimal("100"), Decimal("1")),
        ExecutionReport(
            "fabricated-report",
            STAMP,
            1,
            "fabricated-report",
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
            "fabricated-fill",
            STAMP,
            1,
            "fabricated-fill",
            "order-1",
            AAPL,
            Side.BUY,
            Decimal("2"),
            Decimal("100"),
        ),
        Position(
            "fabricated-position",
            STAMP,
            1,
            "account",
            AAPL,
            Decimal("2"),
            Decimal("100"),
        ),
        Book("fabricated-book", STAMP, 1, AAPL),
        AccountSnapshot(
            "fabricated-account",
            STAMP,
            1,
            "account",
            Decimal("800"),
            Decimal("1000"),
            Decimal("800"),
        ),
    )


def _run_adversary(
    strategy_class: type[Strategy], *, sibling: str | None = None
) -> _Run:
    """Drive one adversary through the kernel-shaped capability harness.

    The harness mirrors the engine shape: every capability is present at
    once (read-only clock, wrapped history, one-way venue, reserved-class
    scheduler, scoped book views).  After the first quote, a genuinely
    future event is queued while a same-head quote is dispatched, so each
    probe round runs both with and without an undispatched future event
    waiting in the queue.
    """
    clock = SimClock(STAMP)
    bus = EventBus(clock)
    hub = PortfolioHub("account")
    hub.create_book("strategy", "1000")
    if sibling is not None:
        hub.create_book(sibling, "1000")
        hub.apply_fill(sibling, _fill("sibling-entry", SPY))
    view = HistoricalView(clock, capacity=64)
    view.subscribe(AAPL, Event)
    view.attach(bus)
    recording_venue = _RecordingVenue()
    context = StrategyContext(
        clock,
        portfolio=hub,
        bus=bus,
        venue=StrategyVenue(recording_venue),
        data=view,
        universe=(AAPL,),
        subscriptions=(QuoteEvent, TradeEvent),
        book_id="strategy",
        seed=1729,
    )
    binding = bind_strategy(strategy_class, {}, context)
    binding.start()
    bus.publish(_quote("q0", STAMP, 0))
    bus.drain(limit=1)
    bus.publish(_quote("q-future", STAMP + timedelta(seconds=5), 0))
    bus.publish(_quote("q1", STAMP, 1))
    bus.drain(limit=2)
    binding.stop()
    return _Run(binding, bus, clock, view, recording_venue, hub)


class ClockAdvanceAdversary(_AttackRecord, Strategy):
    """(a) attempts to advance the engine clock inside every callback."""

    def __init__(self) -> None:
        _AttackRecord.__init__(self)
        self.context: StrategyContext | None = None
        self.observed: list[datetime] = []

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._probe()

    def on_quote(self, event: QuoteEvent) -> None:
        self._probe()

    def _probe(self) -> None:
        context = self.context
        assert context is not None
        clock = context.clock
        self.observed.append(clock.now())
        for name in CLOCK_ABSENT_ATTRIBUTES:
            self._absent("clock", clock, name)
        target = clock.now() + timedelta(days=1)
        self._attempt(
            "advance_to",
            lambda: getattr(clock, "advance_to")(target),  # noqa: B009 - dynamic probe
            AttributeError,
        )
        self._attempt(
            "advance_by",
            lambda: getattr(clock, "advance_by")(  # noqa: B009 - dynamic probe
                timedelta(days=1)
            ),
            AttributeError,
        )
        self._attempt(
            "write_head",
            lambda: setattr(clock, "head", target),  # noqa: B010 - dynamic probe
            AttributeError,
        )
        self._attempt(
            "rebind_now",
            lambda: setattr(clock, "now", lambda: target),  # noqa: B010
            AttributeError,
        )
        self._attempt(
            "rebind_context_clock",
            lambda: setattr(context, "clock", clock),  # noqa: B010
            AttributeError,
        )


class TimeTravelAdversary(_AttackRecord, Strategy):
    """(b) tries timestamped reads, future bounds, and captured re-queries.

    The captured capability is queried again at every later callback, so a
    regression that freezes the as-of or widens the clip would surface as a
    future-dated observation.  The ``# ty: ignore`` markers document where
    the type checker itself rejects the attack; the paired runtime
    assertions pin the signatures as the enforcement backstop.
    """

    def __init__(self) -> None:
        _AttackRecord.__init__(self)
        self.context: StrategyContext | None = None
        self.captured: StrategyHistory | None = None
        self.reads: list[str] = []
        self.windows: list[tuple[str, ...]] = []

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        if not isinstance(context.data, StrategyHistory):
            self.escaped.append("capability:absent")
            return
        self.captured = context.data
        self._probe(None)

    def on_quote(self, event: QuoteEvent) -> None:
        self._probe(event)

    def _probe(self, event: QuoteEvent | None) -> None:
        history = self.captured
        context = self.context
        if history is None or context is None:
            return
        head = context.clock.now()
        future_stamp = head + timedelta(days=1)
        self._attempt(
            "typed-future-read",
            lambda: history.read(
                future_stamp,  # ty: ignore[invalid-argument-type]
                AAPL,  # ty: ignore[invalid-argument-type]
            ),
            TypeError,
        )
        self._attempt(
            "future-window",
            lambda: history.window(
                AAPL, STAMP - timedelta(minutes=1), head + timedelta(seconds=1)
            ),
            LookaheadError,
        )
        self._attempt(
            "future-aggregate",
            lambda: history.aggregate(
                AAPL, STAMP - timedelta(minutes=1), head + timedelta(seconds=1)
            ),
            LookaheadError,
        )
        self._attempt(
            "future-bars",
            lambda: history.bars(
                AAPL, "1min", STAMP - timedelta(minutes=1), head + timedelta(seconds=1)
            ),
            LookaheadError,
        )
        window = history.window(AAPL, STAMP - timedelta(minutes=1))
        self.windows.append(tuple(item.event_id for item in window))
        history.last(AAPL, 10)
        history.aggregate(AAPL, STAMP - timedelta(minutes=1))
        history.bars(AAPL, "1min", STAMP - timedelta(minutes=1))
        if event is None:
            return
        read = history.read(event.symbol)
        if not isinstance(read, QuoteEvent):
            self.violations.append(f"{event.event_id}:missing-read")
            return
        self.reads.append(read.event_id)
        for observed in (read, *window):
            if observed.timestamp > head:
                self.violations.append(f"{event.event_id}:future-observation")


class HistoryPoisonerAdversary(_AttackRecord, Strategy):
    """(c) probes for record, subscribe, attach, and capacity surfaces."""

    def __init__(self) -> None:
        _AttackRecord.__init__(self)
        self.context: StrategyContext | None = None
        self.aliases_agree = True

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._probe()

    def on_quote(self, event: QuoteEvent) -> None:
        self._probe()

    def _probe(self) -> None:
        context = self.context
        assert context is not None
        if context.history is not context.data:
            self.aliases_agree = False
        history = context.data
        if not isinstance(history, StrategyHistory):
            self.escaped.append("capability:absent")
            return
        for name in POISONER_ABSENT_ATTRIBUTES:
            self._absent("history", history, name)
        self._attempt(
            "write_record",
            lambda: setattr(history, "record", lambda candidate: None),  # noqa: B010
            AttributeError,
        )
        self._attempt(
            "write_view",
            lambda: setattr(history, "_view", history),  # noqa: B010
            AttributeError,
        )
        self._attempt(
            "write_clock",
            lambda: setattr(history, "_clock", None),  # noqa: B010
            AttributeError,
        )
        self._attempt(
            "delete_record",
            lambda: delattr(history, "record"),
            AttributeError,
        )


class MarketDataFabricator(_AttackRecord, Strategy):
    """(d) schedules every reserved class through both scheduler surfaces."""

    def __init__(self) -> None:
        _AttackRecord.__init__(self)
        self.context: StrategyContext | None = None
        self._scheduled = 0

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._probe()

    def on_quote(self, event: QuoteEvent) -> None:
        self._probe()

    def _probe(self) -> None:
        context = self.context
        assert context is not None
        for fabricated in _reserved_events():
            self._attempt(
                f"schedule:{type(fabricated).__name__}",
                lambda fabricated=fabricated: context.schedule(fabricated),
                TypeError,
            )
            self._attempt(
                f"scheduler:{type(fabricated).__name__}",
                lambda fabricated=fabricated: context.scheduler.schedule(fabricated),
                TypeError,
            )
        self._scheduled += 1
        stamp = context.clock.now() + timedelta(minutes=1)
        self._permit(
            f"strategy-event-{self._scheduled}",
            lambda scheduled=self._scheduled, stamp=stamp: context.schedule(
                Event(f"s-note-{scheduled}", stamp, 100 + scheduled)
            ),
        )


class VenueStateProber(_AttackRecord, Strategy):
    """(e) probes the one-way venue capability for introspection surfaces."""

    def __init__(self) -> None:
        _AttackRecord.__init__(self)
        self.context: StrategyContext | None = None
        self.cancel_returns: list[object] = []

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._probe()

    def on_quote(self, event: QuoteEvent) -> None:
        self._probe()

    def _probe(self) -> None:
        context = self.context
        assert context is not None
        venue = context.venue
        if venue is None:
            self.escaped.append("venue:absent")
            return
        if context.venue is not context.orders.venue:
            self.violations.append("venue-alias-mismatch")
        for name in VENUE_INTROSPECTION_ATTRIBUTES:
            self._absent("orders", context.orders, name)
            self._absent("venue", venue, name)
        for command in VENUE_COMMANDS:
            if not callable(getattr(venue, command, None)):
                self.escaped.append(f"missing-command:{command}")
        self.cancel_returns.append(venue.cancel("no-such-order"))


class CrossBookSnooper(_AttackRecord, Strategy):
    """(f) probes sibling books through every portfolio view surface."""

    def __init__(self) -> None:
        _AttackRecord.__init__(self)
        self.context: StrategyContext | None = None
        self.own_fill_count: int | None = None
        self.snapshot_book_ids: tuple[str, ...] = ()

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._probe()

    def on_quote(self, event: QuoteEvent) -> None:
        self._probe()

    def _probe(self) -> None:
        context = self.context
        assert context is not None
        self.snapshot_book_ids = tuple(book.book_id for book in context.portfolio.books)
        self.own_fill_count = context.books.current.fill_count
        self._attempt(
            "sibling-book",
            lambda: context.books.book("sibling"),
            UnknownBookError,
        )
        self._attempt(
            "portfolio-sibling-book",
            lambda: context.portfolio.book("sibling"),
            UnknownBookError,
        )
        snapshot = context.account.snapshot
        if isinstance(snapshot, PortfolioSnapshot):
            self._attempt(
                "account-sibling-book",
                lambda: snapshot.book("sibling"),
                UnknownBookError,
            )
        self._permit("own-book", lambda: context.books.book("strategy"))


class TestClockAdvanceSurfaces:
    """Family (a): no strategy-reachable surface moves the engine head."""

    def test_clock_adversary_cannot_advance_any_surface(self) -> None:
        run = _run_adversary(ClockAdvanceAdversary)
        adversary = run.binding.strategy
        assert isinstance(adversary, ClockAdvanceAdversary)

        assert adversary.escaped == []
        assert adversary.blocked == list(CLOCK_PROBE_LABELS) * PROBE_ROUNDS
        assert adversary.observed == [
            STAMP,
            STAMP,
            STAMP,
            STAMP + timedelta(seconds=5),
        ]

    def test_only_the_kernel_queue_moves_the_head(self) -> None:
        run = _run_adversary(ClockAdvanceAdversary)

        assert run.clock.now() == STAMP + timedelta(seconds=5)
        assert run.bus.dispatched_count == 3


class TestFutureDataTimeTravel:
    """Family (b): timestamped reads, future bounds, captured re-queries."""

    def test_time_travel_adversary_never_observes_beyond_the_head(self) -> None:
        run = _run_adversary(TimeTravelAdversary)
        adversary = run.binding.strategy
        assert isinstance(adversary, TimeTravelAdversary)

        assert adversary.escaped == []
        assert adversary.violations == []
        assert adversary.blocked == list(TIME_TRAVEL_LABELS) * PROBE_ROUNDS
        assert adversary.reads == ["q0", "q1", "q-future"]
        # The queued-but-undispatched future event is invisible at the old
        # head; windows are half-open [start, end), so same-head events
        # enter the window only once the engine has advanced past them.
        assert tuple(adversary.windows) == (
            (),
            (),
            (),
            ("q0", "q1"),
        )


class TestDataPoisoning:
    """Family (c): no poisoning surface exists on the data capability."""

    def test_poisoner_cannot_reach_the_engine_view(self) -> None:
        run = _run_adversary(HistoryPoisonerAdversary)
        adversary = run.binding.strategy
        assert isinstance(adversary, HistoryPoisonerAdversary)

        assert adversary.escaped == []
        assert adversary.violations == []
        assert adversary.aliases_agree
        assert adversary.blocked == list(POISONER_LABELS) * PROBE_ROUNDS
        assert run.view.retained(AAPL) == 3
        assert run.view.dropped(AAPL) == 0

    def test_context_wraps_instead_of_exposing_the_engine_view(self) -> None:
        run = _run_adversary(HistoryPoisonerAdversary)
        context = run.binding.context

        assert isinstance(context.data, StrategyHistory)
        assert context.data is not run.view
        assert context.history is context.data


class TestFabricatedMarketData:
    """Family (d): every reserved class is refused on both schedule surfaces."""

    def test_fabricator_cannot_impersonate_feed_or_venue_events(self) -> None:
        run = _run_adversary(MarketDataFabricator)
        adversary = run.binding.strategy
        assert isinstance(adversary, MarketDataFabricator)

        assert adversary.escaped == []
        assert adversary.violations == []
        assert adversary.blocked == list(FABRICATOR_LABELS) * PROBE_ROUNDS
        assert adversary.allowed == [
            f"strategy-event-{index}" for index in range(1, PROBE_ROUNDS + 1)
        ]
        assert all(
            "reserved for the feed" in message
            for message in adversary.messages.values()
        )
        # Only feed events were dispatched; the strategy-owned events stay
        # queued, proving the refusal is targeted rather than a blanket ban.
        assert run.bus.dispatched_count == 3
        assert len(run.bus) == PROBE_ROUNDS


class TestVenueStateIntrospection:
    """Family (e): the one-way command capability carries no venue state."""

    def test_prober_finds_no_introspection_surface(self) -> None:
        run = _run_adversary(VenueStateProber)
        adversary = run.binding.strategy
        assert isinstance(adversary, VenueStateProber)

        assert adversary.escaped == []
        assert adversary.violations == []
        assert adversary.cancel_returns == [None] * PROBE_ROUNDS
        assert run.recording_venue.calls == ["cancel:no-such-order"] * PROBE_ROUNDS
        assert run.bus.dispatched_count == 3

    def test_context_exposes_the_capability_not_the_concrete_venue(self) -> None:
        run = _run_adversary(VenueStateProber)
        venue = run.binding.context.venue

        assert isinstance(venue, StrategyVenue)
        assert venue is not run.recording_venue


class TestCrossBookSnooping:
    """Family (f): sibling books are unrepresentable through the views."""

    def test_snooper_sees_only_its_own_book(self) -> None:
        run = _run_adversary(CrossBookSnooper, sibling="sibling")
        adversary = run.binding.strategy
        assert isinstance(adversary, CrossBookSnooper)

        assert adversary.escaped == []
        assert adversary.violations == []
        assert adversary.blocked == list(SNOOPING_LABELS) * PROBE_ROUNDS
        assert adversary.allowed == ["own-book"] * PROBE_ROUNDS
        assert adversary.snapshot_book_ids == ("strategy",)
        assert adversary.own_fill_count == 0
        assert run.hub.book("sibling").fill_count == 1

    def test_own_book_stays_fully_readable(self) -> None:
        run = _run_adversary(CrossBookSnooper, sibling="sibling")
        context = run.binding.context

        assert context.books.book_id == "strategy"
        assert context.books.book_ids == ("strategy",)
        assert context.books.book("strategy").book_id == "strategy"
        assert context.books.current.cash == Decimal("1000")
