"""PaperShell: the shared simulation runtime for long-running paper trading.

PaperShell composes the deterministic backtest engine capabilities — the
SimClock-driven kernel, the simulated venue with pessimistic fill plugins,
fee schedules, the pre-trade risk pipeline, and portfolio accounting — against
a live or replay market-data boundary instead of a replay fixture.  It is a
composition, not a second engine: the same event kernel, venue, fill models,
fees, risk rules, and virtual books that produce backtest ledgers drive paper
sessions, so identical event streams and fill configuration produce equivalent
paper and backtest ledgers.

The synchronous kernel never reads wall time, unseeded randomness, or the
network.  Wall-clock time enters only through the explicit ``wall_clock``
adapter boundary, which stamps session boot and shutdown provenance, and
through an optional deployment-supplied idle pace callback between boundary
polls.  Market events arrive through a :class:`PaperFeed` boundary —
:class:`ReplayPaperFeed` for deterministic lake replay and
:class:`InboxPaperFeed` for pop-style kernel-facing inboxes — and
market-session boot and shutdown behavior is governed by
:class:`PaperSessionPolicy`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, cast, runtime_checkable

from harness.configuration import TraderConfig, canonical_hash
from harness.core.clock import Clock, ReadonlyClock, SimClock, WallClock
from harness.core.events import EventBus
from harness.core.types import (
    Event,
    ExecutionReport,
    ExecutionType,
    Fill,
    OrderRequest,
    OrderStatus,
    QuoteEvent,
    SingleLegOrder,
    TradeEvent,
)
from harness.core.types.common import (
    DecimalInput,
    exact_decimal,
    text,
    timestamp,
)
from harness.data import (
    DEFAULT_HISTORY_CAPACITY,
    HistoricalView,
    LakeReplayFeed,
    MarketEvent,
    ReplayFingerprints,
)
from harness.data.live_recorder import LiveRecorder
from harness.fills import AssetClass as FeeAssetClass
from harness.fills import (
    FeeAggregation,
    FeeAssessment,
    FeeContext,
    FeeSchedule,
    aggregate_fees,
)
from harness.portfolio import PortfolioHub, PortfolioSnapshot, VirtualBook
from harness.risk import RiskDecision, RiskPipeline
from harness.runtime.backtest import code_fingerprint
from harness.runtime.backtest.engine import _SUBSCRIPTIONS
from harness.runtime.backtest.errors import (
    _ZERO,
    AbortPolicy,
    BacktestAborted,
    LookaheadViolationError,
    StrategyExecutionError,
    StrategyHalt,
    StrategyHaltPolicy,
    TeardownPolicy,
    _LoopStop,
)
from harness.runtime.backtest.fees import (
    DEFAULT_FEE_PRECISION,
    fee_schedule_from_config,
)
from harness.runtime.backtest.fills import (
    FillModel,
    FillModelOptions,
    build_fill_model,
    fill_model_identity,
    fill_model_latency,
)
from harness.runtime.backtest.manifest import (
    _fees_json,
    _fill_json,
    _portfolio_json,
    _risk_json,
)
from harness.runtime.backtest.strategies import (
    StrategyLoader,
    _GuardedBinding,
    _RiskVenue,
    _StrategySink,
    default_strategy_loader,
)
from harness.strategy import Strategy, StrategyContext
from harness.venues import SimulatedVenue
from lake.contracts import ExchangeSession

UNATTRIBUTED = "unattributed"


# --------------------------------------------------------------------------
# Errors, policies, and options
# --------------------------------------------------------------------------


class PaperShellError(RuntimeError):
    """Base class for every paper shell error."""


class PaperConfigurationError(PaperShellError, ValueError):
    """Raised when the supplied configuration cannot drive a paper session."""


class PaperSessionPolicy(StrEnum):
    """Market-session boot and shutdown behavior for one paper session.

    ``SESSION_WINDOW`` boots the session at the declared open, drops every
    event outside the window, and keeps running until the boundary is
    exhausted or :meth:`PaperShell.stop` is requested.  ``STOP_AT_CLOSE``
    boots at the open and shuts the session down cleanly with the first event
    at or after the declared close.  ``OPEN_ENDED`` admits every event and is
    the policy for continuous sessions such as crypto.
    """

    SESSION_WINDOW = "session_window"
    STOP_AT_CLOSE = "stop_at_close"
    OPEN_ENDED = "open_ended"


class PaperSessionStatus(StrEnum):
    """Terminal status of one paper session."""

    COMPLETED = "completed"
    STOPPED = "stopped"
    AT_CLOSE = "at_close"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class VenueWatermarks:
    """Venue id and event-sequence counters at one supervision point.

    A restored runtime rebuilds its simulated venue fresh, so the counters of
    the captured runtime must be replayed into the new kernel before it
    generates outcomes; otherwise the venue regenerates fill identifiers and
    event sequences the restored books already carry.  ``id_ordinal`` is the
    last used venue identifier ordinal and ``event_sequence`` the last used
    event sequence (``-1`` before any event).
    """

    id_ordinal: int
    event_sequence: int

    def __post_init__(self) -> None:
        for name, value, floor in (
            ("id_ordinal", self.id_ordinal, 0),
            ("event_sequence", self.event_sequence, -1),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise PaperConfigurationError(f"{name} must be an integer")
            if value < floor:
                raise PaperConfigurationError(f"{name} must not precede {floor}")


def _validate_session(session: ExchangeSession) -> None:
    if session.open_time.tzinfo is None or session.close_time.tzinfo is None:
        raise PaperConfigurationError("session bounds must be timezone-aware")
    if session.close_time <= session.open_time:
        raise PaperConfigurationError("session close_time must be after open_time")


@dataclass(frozen=True, slots=True)
class PaperOptions:
    """Explicit knobs and the session declaration for one paper session."""

    run_id: str
    initial_cash: DecimalInput
    fill_model: FillModel | str = FillModel.SIMPLE_TOUCH
    fill_options: FillModelOptions | None = None
    fee_precision: Decimal = DEFAULT_FEE_PRECISION
    asset_class: FeeAssetClass = FeeAssetClass.EQUITY
    history_capacity: int = DEFAULT_HISTORY_CAPACITY
    abort_policy: AbortPolicy = AbortPolicy.STOP
    strategy_halt_policy: StrategyHaltPolicy = StrategyHaltPolicy.HALT_STRATEGY
    teardown_policy: TeardownPolicy = TeardownPolicy.ALWAYS
    extra_latency: timedelta = timedelta(0)
    session_policy: PaperSessionPolicy = PaperSessionPolicy.OPEN_ENDED
    session: ExchangeSession | None = None
    start: datetime | None = None
    idle_pace: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        text(self.run_id, "run_id")
        cash = exact_decimal(self.initial_cash, "initial_cash")
        if cash <= _ZERO:
            raise PaperConfigurationError("initial_cash must be positive")
        if not isinstance(self.extra_latency, timedelta):
            raise PaperConfigurationError("extra_latency must be a timedelta")
        if self.extra_latency < timedelta(0):
            raise PaperConfigurationError("extra_latency must not be negative")
        if self.session_policy is PaperSessionPolicy.OPEN_ENDED:
            if self.session is not None:
                raise PaperConfigurationError(
                    "an open-ended session cannot declare a session window"
                )
            if self.start is None:
                raise PaperConfigurationError(
                    "a paper session requires a session window or an explicit "
                    "event-time start"
                )
            timestamp(self.start, "start")
        else:
            if self.session is None:
                raise PaperConfigurationError(
                    f"{self.session_policy.value} sessions require a declared "
                    "session window"
                )
            _validate_session(self.session)
            if self.start is not None:
                raise PaperConfigurationError(
                    "provide either a session window or an explicit start, not both"
                )
        if self.idle_pace is not None and not callable(self.idle_pace):
            raise PaperConfigurationError("idle_pace must be callable")

    @property
    def effective_start(self) -> datetime:
        """Return the event-time boot instant for the session clock."""
        if self.session is not None:
            return self.session.open_time
        assert self.start is not None
        return self.start


# --------------------------------------------------------------------------
# Live and replay data boundaries
# --------------------------------------------------------------------------


@runtime_checkable
class PaperFeed(Protocol):
    """Kernel-facing live/replay market-event boundary for one session.

    ``next_event`` returns the next market event, or ``None`` while the
    boundary has nothing available.  ``exhausted`` reports whether the
    boundary will ever produce further events: replay boundaries exhaust,
    live boundaries never do and keep a session running until it is stopped
    or reaches its session close.
    """

    def next_event(self) -> MarketEvent | None:
        """Return the next market event, or ``None`` while none is available."""
        ...

    @property
    def exhausted(self) -> bool:
        """Return whether the boundary has no further events."""
        ...

    def close(self) -> None:
        """Release the boundary's resources; idempotent."""
        ...


@runtime_checkable
class PaperInbox(Protocol):
    """Pop-style kernel-facing inbox, such as the Alpaca feed inboxes."""

    def pop(self) -> MarketEvent | None:
        """Remove and return the oldest normalized event, if one is available."""
        ...


class ReplayPaperFeed:
    """Deterministic replay boundary over a :class:`LakeReplayFeed`."""

    __slots__ = ("_exhausted", "_feed", "_iterator")

    def __init__(self, feed: LakeReplayFeed) -> None:
        if not isinstance(feed, LakeReplayFeed):
            raise PaperConfigurationError("feed must be a LakeReplayFeed")
        self._feed = feed
        self._iterator = iter(feed)
        self._exhausted = False

    @property
    def exhausted(self) -> bool:
        """Return whether the replay has no further events."""
        return self._exhausted

    @property
    def fingerprints(self) -> ReplayFingerprints:
        """Return the replay fingerprints reported by the wrapped feed."""
        return self._feed.fingerprints

    def next_event(self) -> MarketEvent | None:
        """Return the next merged replay event, or ``None`` once exhausted."""
        try:
            return next(self._iterator)
        except StopIteration:
            self._exhausted = True
            return None

    def close(self) -> None:
        """Close the underlying replay iterator; idempotent."""
        close = getattr(self._iterator, "close", None)
        if callable(close):
            close()
        self._exhausted = True


class InboxPaperFeed:
    """Live boundary over a pop-style kernel-facing inbox.

    The boundary never exhausts: a live session runs until it is stopped or
    reaches its declared session close, and idle polls return ``None``.
    """

    __slots__ = ("_inbox",)

    def __init__(self, inbox: PaperInbox) -> None:
        if not isinstance(inbox, PaperInbox):
            raise PaperConfigurationError("inbox must provide pop()")
        self._inbox = inbox

    @property
    def exhausted(self) -> bool:
        """Return ``False``: live boundaries never exhaust."""
        return False

    def next_event(self) -> MarketEvent | None:
        """Return the next inbox event, or ``None`` while the inbox is idle."""
        return self._inbox.pop()

    def close(self) -> None:
        """Inboxes own no kernel resources; closing is a no-op."""
        return None


# --------------------------------------------------------------------------
# Session result
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaperSessionResult:
    """One paper session outcome with its ledger and provenance artifacts."""

    run_id: str
    mode: str
    status: PaperSessionStatus
    reason: str | None
    session: ExchangeSession | None
    session_policy: PaperSessionPolicy
    booted_at: datetime
    ended_at: datetime | None
    last_event_time: datetime | None
    dispatch_hash: str
    dispatched_count: int
    market_event_count: int
    out_of_session_events: int
    order_count: int
    fills: tuple[Fill, ...]
    fee_assessments: tuple[FeeAssessment, ...]
    fee_totals: FeeAggregation
    portfolio: PortfolioSnapshot
    risk_decisions: tuple[RiskDecision, ...]
    halted_strategies: tuple[str, ...]
    stopped_strategies: tuple[str, ...]
    teardown_errors: tuple[str, ...]
    lookahead_violations: tuple[str, ...]
    recorded_rows: Mapping[str, int] | None
    feed_fingerprints: ReplayFingerprints | None
    fill_model: str
    fill_model_fingerprint: str
    engine_fingerprint: str

    @property
    def completed(self) -> bool:
        """Return whether the session drained its boundary to exhaustion."""
        return self.status is PaperSessionStatus.COMPLETED

    def artifacts(self) -> dict[str, object]:
        """Return the canonical, JSON-ready session artifacts."""
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status.value,
            "reason": self.reason,
            "session": _session_json(self.session),
            "session_policy": self.session_policy.value,
            "booted_at": _iso(self.booted_at),
            "ended_at": _iso_or_none(self.ended_at),
            "last_event_time": _iso_or_none(self.last_event_time),
            "dispatch_hash": self.dispatch_hash,
            "counts": {
                "dispatched": self.dispatched_count,
                "market_events": self.market_event_count,
                "out_of_session": self.out_of_session_events,
                "orders": self.order_count,
                "fills": len(self.fills),
                "risk_decisions": len(self.risk_decisions),
            },
            "strategies": {
                "halted": list(self.halted_strategies),
                "stopped": list(self.stopped_strategies),
                "teardown_errors": list(self.teardown_errors),
                "lookahead_violations": list(self.lookahead_violations),
            },
            "fills": [_fill_json(fill) for fill in self.fills],
            "fees": _fees_json(self.fee_assessments, self.fee_totals),
            "portfolio": _portfolio_json(self.portfolio),
            "risk": [_risk_json(decision) for decision in self.risk_decisions],
            "recorded_rows": (
                None if self.recorded_rows is None else dict(self.recorded_rows)
            ),
            "feed_fingerprints": _fingerprints_json(self.feed_fingerprints),
            "fill_model": self.fill_model,
            "fill_model_fingerprint": self.fill_model_fingerprint,
            "engine_fingerprint": self.engine_fingerprint,
        }


def _session_json(session: ExchangeSession | None) -> dict[str, object] | None:
    if session is None:
        return None
    return {
        "session_date": session.session_date.isoformat(),
        "open_time": _iso(session.open_time),
        "close_time": _iso(session.close_time),
    }


def _fingerprints_json(
    fingerprints: ReplayFingerprints | None,
) -> dict[str, object] | None:
    if fingerprints is None:
        return None
    return {
        "file": fingerprints.file,
        "row": fingerprints.row,
        "data": fingerprints.data,
    }


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


def _order_quantity(order: OrderRequest) -> Decimal:
    if isinstance(order, SingleLegOrder):
        return order.quantity
    return sum((leg.quantity for leg in order.legs), start=_ZERO)


# --------------------------------------------------------------------------
# Shell
# --------------------------------------------------------------------------


class PaperShell:
    """Compose the shared simulation capabilities into one paper session.

    The shell binds every configured strategy to its own virtual book, runs
    one :class:`~harness.core.clock.SimClock`-driven
    :class:`~harness.core.events.EventBus` kernel with a
    :class:`~harness.venues.sim.SimulatedVenue`, applies fees on fills,
    evaluates the pre-trade risk pipeline through the same guarded venue
    capability as backtests, and routes multi-strategy fills across virtual
    books.  Session boot and shutdown provenance is stamped by the injected
    ``wall_clock`` adapter; the kernel itself stays event-time deterministic.
    """

    __slots__ = (
        "_abort_error",
        "_abort_policy",
        "_assessments",
        "_at_close",
        "_bindings",
        "_booted_at",
        "_bus",
        "_clock",
        "_configuration",
        "_ended_at",
        "_fee_asset_class",
        "_fee_schedule",
        "_fill_model",
        "_fill_model_fingerprint",
        "_feed_fingerprints",
        "_fills",
        "_finalized",
        "_halt_policy",
        "_halted",
        "_hub",
        "_initial_cash",
        "_last_event_time",
        "_last_sequence",
        "_market_events",
        "_options",
        "_order_counts",
        "_order_owners",
        "_out_of_session",
        "_pipeline",
        "_prices",
        "_recorded_rows",
        "_recorder",
        "_reject_ordinal",
        "_result",
        "_risk_decisions",
        "_run_id",
        "_running",
        "_session",
        "_session_policy",
        "_stop_reason",
        "_stopped",
        "_teardown_errors",
        "_teardown_policy",
        "_venue",
        "_view",
        "_violations",
        "_wall_clock",
    )

    def __init__(
        self,
        configuration: TraderConfig,
        *,
        options: PaperOptions,
        fee_schedule: FeeSchedule | None = None,
        risk_pipeline: RiskPipeline | None = None,
        strategy_loader: StrategyLoader | None = None,
        recorder: LiveRecorder | None = None,
        wall_clock: Clock | None = None,
    ) -> None:
        _require_paper_config(configuration)
        if not isinstance(options, PaperOptions):
            raise PaperConfigurationError("options must be a PaperOptions")
        self._configuration = configuration
        self._options = options
        self._session = options.session
        self._session_policy = options.session_policy
        self._run_id = text(options.run_id, "run_id")
        self._initial_cash = exact_decimal(options.initial_cash, "initial_cash")
        self._abort_policy = options.abort_policy
        self._halt_policy = options.strategy_halt_policy
        self._teardown_policy = options.teardown_policy
        self._pipeline = (
            RiskPipeline(configuration.risk_pipeline)
            if risk_pipeline is None
            else risk_pipeline
        )
        if not isinstance(self._pipeline, RiskPipeline):
            raise PaperConfigurationError("risk_pipeline must be a RiskPipeline")
        schedule = (
            fee_schedule_from_config(
                configuration.fees, precision=options.fee_precision
            )
            if fee_schedule is None
            else fee_schedule
        )
        if not isinstance(schedule, FeeSchedule):
            raise PaperConfigurationError("fee_schedule must be a FeeSchedule")
        self._fee_schedule = schedule
        self._fee_asset_class = options.asset_class
        self._recorder = recorder
        if recorder is not None and not isinstance(recorder, LiveRecorder):
            raise PaperConfigurationError("recorder must be a LiveRecorder")
        self._wall_clock = WallClock() if wall_clock is None else wall_clock
        if not isinstance(self._wall_clock, Clock):
            raise PaperConfigurationError("wall_clock must implement Clock")

        self._finalized = False
        self._running = False
        self._at_close = False
        self._stop_reason: str | None = None
        self._booted_at = self._wall_clock.now()
        self._ended_at: datetime | None = None
        self._result: PaperSessionResult | None = None
        self._last_sequence = 0
        self._reject_ordinal = 0
        self._market_events = 0
        self._out_of_session = 0
        self._fills: list[Fill] = []
        self._assessments: list[FeeAssessment] = []
        self._risk_decisions: list[RiskDecision] = []
        self._halted: list[str] = []
        self._stopped: list[str] = []
        self._teardown_errors: list[str] = []
        self._order_counts: dict[str, int] = {}
        self._order_owners: dict[str, str] = {}
        self._prices: dict[str, Decimal] = {}
        self._violations: list[str] = []
        self._abort_error: Exception | None = None
        self._last_event_time: datetime | None = None
        self._recorded_rows: dict[str, int] | None = None
        self._feed_fingerprints: ReplayFingerprints | None = None
        self._bindings: list[_GuardedBinding] = []

        self._clock = SimClock(options.effective_start)
        self._bus = EventBus(self._clock)
        self._hub = PortfolioHub(self._run_id)
        for instance in configuration.strategy_instances:
            self._hub.create_book(
                instance.instance_id,
                self._initial_cash * instance.capital_fraction,
            )
        plugin = build_fill_model(options.fill_model, options.fill_options)
        identity = fill_model_identity(plugin)
        self._fill_model = str(identity["model"])
        self._fill_model_fingerprint = canonical_hash(identity)
        self._venue: SimulatedVenue = SimulatedVenue(
            self._bus,
            fill_model=plugin,
            clock=self._clock,
            latency=fill_model_latency(plugin) + options.extra_latency,
        )
        self._view: HistoricalView | None = None
        if options.history_capacity > 0:
            view = HistoricalView(self._clock, capacity=options.history_capacity)
            for symbol in _session_symbols(configuration):
                view.subscribe(symbol)
            view.attach(self._bus)
            self._view = view

        self._bus.subscribe(Event, self._observe)
        self._bus.subscribe(QuoteEvent, self._on_market)
        self._bus.subscribe(TradeEvent, self._on_market)
        self._bus.subscribe(Fill, self._on_fill)
        self._bind_strategies(strategy_loader)

    # -- kernel state exposed to the risk gate and supervision --------------

    @property
    def pipeline(self) -> RiskPipeline:
        """Return the configured pre-trade risk pipeline."""
        return self._pipeline

    @property
    def hub(self) -> PortfolioHub:
        """Return the shared portfolio hub with one book per strategy."""
        return self._hub

    @property
    def prices(self) -> Mapping[str, Decimal]:
        """Return the latest exact mark per canonical symbol."""
        return self._prices

    @property
    def run_id(self) -> str:
        """Return the declared session run identifier."""
        return self._run_id

    @property
    def running(self) -> bool:
        """Return whether :meth:`run` is currently driving the kernel."""
        return self._running

    @property
    def finalized(self) -> bool:
        """Return whether the session has been torn down and finalized."""
        return self._finalized

    def daily_loss(self, instance_id: str) -> Decimal:
        """Return one strategy's exact loss against its opening allocation."""
        book: VirtualBook = self._hub.book(instance_id)
        equity = book.equity
        if equity is None:
            return _ZERO
        opening = book.initial_cash
        return opening - equity if equity < opening else _ZERO

    def order_count(self, instance_id: str) -> int:
        """Return how many orders one strategy has submitted so far."""
        return self._order_counts.get(instance_id, 0)

    def count_order(self, instance_id: str) -> None:
        """Record one submitted order for a strategy."""
        self._order_counts[instance_id] = self.order_count(instance_id) + 1

    def own_order(self, order_id: str, instance_id: str) -> None:
        """Attribute one order identifier to its submitting strategy."""
        self._order_owners[order_id] = instance_id

    def record_risk(self, decision: RiskDecision) -> None:
        """Record one complete risk decision."""
        self._risk_decisions.append(decision)

    def publish_rejection(
        self,
        order: OrderRequest,
        decision: RiskDecision,
        *,
        reason: str | None = None,
    ) -> None:
        """Schedule one rejected execution report for a vetoed order."""
        quantity = _order_quantity(order)
        self._reject_ordinal += 1
        report_id = f"{self._run_id}-reject-{self._reject_ordinal}"
        rejection_reason = reason if reason is not None else decision.reason
        self._bus.schedule(
            ExecutionReport(
                event_id=report_id,
                timestamp=self._clock.now(),
                sequence=self._last_sequence + 1,
                report_id=report_id,
                order_id=str(getattr(order, "order_id", "")),
                status=OrderStatus.REJECTED,
                execution_type=ExecutionType.REJECTED,
                order_quantity=quantity,
                filled_quantity=_ZERO,
                remaining_quantity=quantity,
                text=rejection_reason,
            )
        )

    # -- restore -------------------------------------------------------------

    def strategy_state_export(self) -> dict[str, Mapping[str, object]]:
        """Return each binding's frozen ``StateStore`` values keyed by book id.

        Strategies export their restorable decision state through
        ``context.state``; instances whose store is empty do not participate
        and are omitted, so the export stays stable across restarts.
        """
        exported: dict[str, Mapping[str, object]] = {}
        for binding in self._bindings:
            values: dict[str, object] = dict(binding.context.state.snapshot)
            if values:
                exported[binding.context.book_id] = values
        return exported

    def restore_strategy_state(
        self, states: Mapping[str, Mapping[str, object]]
    ) -> None:
        """Replay captured strategy state into each bound strategy's store.

        Called on a freshly restored shell, this rehydrates every strategy
        that exported state at the capture point, so bindings restarted by the
        restore continue their decisions instead of repeating them.
        """
        for binding in self._bindings:
            values = states.get(binding.context.book_id)
            if values is None:
                continue
            store = binding.context.state
            store.clear()
            for key, value in values.items():
                store[key] = value

    def venue_watermarks(self) -> VenueWatermarks:
        """Return the venue id and event-sequence counters for supervision."""
        return VenueWatermarks(
            id_ordinal=self._venue.id_ordinal,
            event_sequence=self._venue.event_sequence,
        )

    def restore_venue_watermarks(self, watermarks: VenueWatermarks) -> None:
        """Continue venue ids and event sequences past restored runtime state.

        The counters only move forward, and the shell's event watermark rises
        with the venue sequence so supervisory reports cannot reuse sequences
        the restored books already carry.
        """
        if not isinstance(watermarks, VenueWatermarks):
            raise PaperConfigurationError("watermarks must be a VenueWatermarks")
        self._venue.resume_from(
            id_ordinal=watermarks.id_ordinal,
            event_sequence=watermarks.event_sequence,
        )
        if watermarks.event_sequence > self._last_sequence:
            self._last_sequence = watermarks.event_sequence

    # -- session driving ----------------------------------------------------

    def step(self, event: MarketEvent) -> None:
        """Admit one boundary event and run it through the synchronous kernel.

        Session admission policies apply exactly as in :meth:`run`.  The step
        raises :class:`PaperShellError` once the session is finalized,
        stopped, or aborted.
        """
        self._require_steppable()
        if not isinstance(event, QuoteEvent | TradeEvent):
            raise PaperConfigurationError("event must be a QuoteEvent or TradeEvent")
        if not self._admit(event):
            return
        try:
            self._process(event)
        except _LoopStop:
            pass

    def stop(self, reason: str | None = None) -> None:
        """Request a clean shutdown at the end of the current unit of work."""
        if self._stop_reason is None:
            self._stop_reason = reason if reason is not None else "stop requested"

    def run(self, feed: PaperFeed) -> PaperSessionResult:
        """Drive the kernel from one live/replay boundary until the session ends.

        The session ends when the boundary is exhausted, the session-close
        policy fires, :meth:`stop` is requested, or the kernel aborts.  The
        boundary is closed when the session finalizes.
        """
        self._require_runnable()
        if not isinstance(feed, PaperFeed):
            raise PaperConfigurationError("feed must implement PaperFeed")
        self._running = True
        try:
            try:
                self._run_loop(feed)
            except _LoopStop:
                pass
            except Exception as error:
                if self._abort_error is None:
                    self._abort_error = error
            finally:
                self._teardown()
            self._finalize(feed)
        finally:
            self._running = False
        self._raise_abort()
        assert self._result is not None
        return self._result

    def close(self) -> PaperSessionResult:
        """Stop, tear down, and finalize the session; idempotent."""
        self.stop()
        if not self._finalized:
            self._teardown()
            self._finalize(None)
        return self.result()

    def result(self) -> PaperSessionResult:
        """Return the session result, building an in-flight snapshot if needed."""
        if self._result is not None:
            return self._result
        return self._build_result()

    def _require_runnable(self) -> None:
        if self._finalized:
            raise PaperShellError("the paper session is already finalized")
        if self._running:
            raise PaperShellError("the paper session is already running")

    def _require_steppable(self) -> None:
        if self._finalized:
            raise PaperShellError("the paper session is already finalized")
        if self._abort_error is not None:
            raise PaperShellError(
                f"the paper session aborted: {self._abort_error}"
            ) from self._abort_error
        if self._stop_reason is not None:
            raise PaperShellError(
                "the paper session has been stopped; no further steps are accepted"
            )

    def _run_loop(self, feed: PaperFeed) -> None:
        while True:
            if self._stop_reason is not None:
                return
            if feed.exhausted:
                return
            event = feed.next_event()
            if event is None:
                pace = self._options.idle_pace
                if pace is not None:
                    pace()
                continue
            if not self._admit(event):
                continue
            self._process(event)

    def _admit(self, event: MarketEvent) -> bool:
        session = self._session
        if session is None:
            self._record_market(event)
            return True
        if event.timestamp < session.open_time:
            self._out_of_session += 1
            return False
        if event.timestamp >= session.close_time:
            if self._session_policy is PaperSessionPolicy.STOP_AT_CLOSE:
                if self._stop_reason is None:
                    self._stop_reason = (
                        f"session {session.session_date.isoformat()} closed at "
                        f"{_iso(session.close_time)}"
                    )
                self._at_close = True
                return False
            self._out_of_session += 1
            return False
        self._record_market(event)
        return True

    def _record_market(self, event: MarketEvent) -> None:
        if self._recorder is not None:
            self._recorder.record(event)

    def _process(self, event: MarketEvent) -> None:
        self._last_event_time = event.timestamp
        self._bus.publish(event)
        self._market_events += 1
        self._drain()
        self._settle()

    def _drain(self) -> None:
        while True:
            if self._bus.dispatch_next() is None:
                return
            self._settle()

    # -- strategy lifecycle -------------------------------------------------

    def _bind_strategies(self, strategy_loader: StrategyLoader | None) -> None:
        loader = default_strategy_loader if strategy_loader is None else strategy_loader
        for instance in self._configuration.strategy_instances:
            sink = _StrategySink(instance.instance_id)
            strategy_class = loader(instance)
            if not isinstance(strategy_class, type) or not issubclass(
                strategy_class, Strategy
            ):
                raise PaperConfigurationError(
                    f"strategy loader returned an invalid class for "
                    f"{instance.instance_id!r}"
                )
            context = StrategyContext(
                ReadonlyClock(self._clock),
                portfolio=self._hub,
                venue=_RiskVenue(cast("Any", self), instance.instance_id, self._venue),
                bus=self._bus,
                data=self._view,
                seed=self._configuration.seed,
                universe=instance.symbols,
                subscriptions=_SUBSCRIPTIONS,
                book_id=instance.instance_id,
            )
            self._bindings.append(
                _GuardedBinding.bind(
                    strategy_class, dict(instance.parameters), context, sink
                )
            )
        for binding in self._bindings:
            binding.start()

    def _settle(self) -> None:
        for binding in self._bindings:
            sink = binding.sink
            if sink.abort is not None:
                self._abort(
                    BacktestAborted(
                        f"strategy {sink.instance_id!r} aborted the run: {sink.abort}"
                    )
                )
            elif sink.violation is not None:
                self._lookahead(sink)
            elif sink.error is not None:
                self._strategy_error(sink)
            elif sink.halt is not None:
                self._halt(binding)

    def _abort(self, error: Exception) -> None:
        if self._abort_error is None:
            self._abort_error = error
        raise _LoopStop

    def _lookahead(self, sink: _StrategySink) -> None:
        violation = sink.violation
        sink.violation = None
        if sink.instance_id not in self._violations:
            self._violations.append(sink.instance_id)
        self._abort(
            LookaheadViolationError(
                f"strategy {sink.instance_id!r} read data ahead of the engine "
                f"clock: {violation}"
            )
        )

    def _strategy_error(self, sink: _StrategySink) -> None:
        error = sink.error
        sink.error = None
        if error is None:
            return
        raised = StrategyExecutionError(
            f"strategy {sink.instance_id!r} raised {type(error).__name__}: {error}"
        )
        raised.__cause__ = error
        raised.with_traceback(error.__traceback__)
        self._abort(raised)

    def _halt(self, binding: _GuardedBinding) -> None:
        sink = binding.sink
        error = sink.halt
        sink.clear_requests()
        if self._halt_policy is StrategyHaltPolicy.RAISE:
            self._abort(error if error is not None else StrategyHalt("halt requested"))
        if self._halt_policy is StrategyHaltPolicy.ABORT_RUN:
            self._abort(
                BacktestAborted(
                    f"strategy {sink.instance_id!r} halted the run: {error}"
                )
            )
        if sink.instance_id not in self._halted:
            self._halted.append(sink.instance_id)
        self._stop(binding)

    def _stop(self, binding: _GuardedBinding) -> None:
        sink = binding.sink
        binding.stop()
        if sink.stopped and sink.instance_id not in self._stopped:
            self._stopped.append(sink.instance_id)
        if sink.teardown_error is not None and sink.instance_id not in (
            self._teardown_errors
        ):
            self._teardown_errors.append(sink.instance_id)

    def _teardown(self) -> None:
        policy = self._teardown_policy
        if policy is TeardownPolicy.NEVER:
            return
        if policy is TeardownPolicy.ON_SUCCESS and self._abort_error is not None:
            return
        for binding in self._bindings:
            if not binding.started:
                continue
            self._stop(binding)

    def _raise_abort(self) -> None:
        error = self._abort_error
        if error is None or self._abort_policy is not AbortPolicy.RAISE:
            return
        raise error

    # -- bus handlers -------------------------------------------------------

    def _observe(self, event: Event) -> None:
        if event.sequence > self._last_sequence:
            self._last_sequence = event.sequence

    def _on_market(self, event: QuoteEvent | TradeEvent) -> None:
        if isinstance(event, TradeEvent):
            price = event.price
        else:
            price = (event.bid_price + event.ask_price) / 2
        self._prices[str(event.symbol)] = price
        self._hub.mark(event.symbol, price)

    def _on_fill(self, event: Fill) -> None:
        owner = self._order_owners.get(event.order_id)
        strategy_id = UNATTRIBUTED if owner is None else owner
        assessment = self._fee_schedule.calculate(
            event,
            FeeContext(
                run_id=self._run_id,
                strategy_id=strategy_id,
                asset_class=self._fee_asset_class,
            ),
        )
        self._assessments.append(assessment)
        booked = replace(event, fee=assessment.total)
        if owner is not None:
            self._hub.apply_fill(owner, booked)
        self._fills.append(booked)

    # -- finalization -------------------------------------------------------

    def _finalize(self, feed: PaperFeed | None) -> None:
        if self._finalized:
            return
        if feed is not None:
            feed.close()
            fingerprints = getattr(feed, "fingerprints", None)
            if isinstance(fingerprints, ReplayFingerprints):
                self._feed_fingerprints = fingerprints
        if self._recorder is not None:
            self._recorder.close()
            self._recorded_rows = dict(self._recorder.row_counts)
        self._ended_at = self._wall_clock.now()
        self._finalized = True
        self._running = False
        self._result = self._build_result()

    def _build_result(self) -> PaperSessionResult:
        return PaperSessionResult(
            run_id=self._run_id,
            mode=self._configuration.mode,
            status=self._status(),
            reason=self._reason(),
            session=self._session,
            session_policy=self._session_policy,
            booted_at=self._booted_at,
            ended_at=self._ended_at,
            last_event_time=self._last_event_time,
            dispatch_hash=self._bus.dispatch_hash,
            dispatched_count=self._bus.dispatched_count,
            market_event_count=self._market_events,
            out_of_session_events=self._out_of_session,
            order_count=sum(self._order_counts.values()),
            fills=tuple(self._fills),
            fee_assessments=tuple(self._assessments),
            fee_totals=aggregate_fees(self._assessments),
            portfolio=self._hub.snapshot,
            risk_decisions=tuple(self._risk_decisions),
            halted_strategies=tuple(self._halted),
            stopped_strategies=tuple(self._stopped),
            teardown_errors=tuple(self._teardown_errors),
            lookahead_violations=tuple(self._violations),
            recorded_rows=(
                None if self._recorded_rows is None else dict(self._recorded_rows)
            ),
            feed_fingerprints=self._feed_fingerprints,
            fill_model=self._fill_model,
            fill_model_fingerprint=self._fill_model_fingerprint,
            engine_fingerprint=code_fingerprint(),
        )

    def _status(self) -> PaperSessionStatus:
        if self._abort_error is not None:
            return PaperSessionStatus.ABORTED
        if self._at_close:
            return PaperSessionStatus.AT_CLOSE
        if self._stop_reason is not None:
            return PaperSessionStatus.STOPPED
        return PaperSessionStatus.COMPLETED

    def _reason(self) -> str | None:
        if self._abort_error is not None:
            return str(self._abort_error)
        return self._stop_reason


def _require_paper_config(configuration: TraderConfig) -> None:
    if not isinstance(configuration, TraderConfig):
        raise PaperConfigurationError("configuration must be a TraderConfig")
    if configuration.mode != "paper":
        raise PaperConfigurationError(
            "PaperShell requires a trader configuration in paper mode, got "
            f"mode {configuration.mode!r}"
        )


def _session_symbols(configuration: TraderConfig) -> tuple[str, ...]:
    symbols: set[str] = set()
    for instance in configuration.strategy_instances:
        symbols.update(str(symbol) for symbol in instance.symbols)
    return tuple(sorted(symbols))


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------


def run_paper_session(
    configuration: TraderConfig,
    feed: PaperFeed,
    *,
    options: PaperOptions,
    fee_schedule: FeeSchedule | None = None,
    risk_pipeline: RiskPipeline | None = None,
    strategy_loader: StrategyLoader | None = None,
    recorder: LiveRecorder | None = None,
    wall_clock: Clock | None = None,
) -> PaperSessionResult:
    """Compose one paper session, drive it from ``feed``, and return its result."""
    shell = PaperShell(
        configuration,
        options=options,
        fee_schedule=fee_schedule,
        risk_pipeline=risk_pipeline,
        strategy_loader=strategy_loader,
        recorder=recorder,
        wall_clock=wall_clock,
    )
    return shell.run(feed)


__all__ = [
    "InboxPaperFeed",
    "PaperConfigurationError",
    "PaperFeed",
    "PaperInbox",
    "PaperOptions",
    "PaperSessionPolicy",
    "PaperSessionResult",
    "PaperSessionStatus",
    "PaperShell",
    "PaperShellError",
    "ReplayPaperFeed",
    "VenueWatermarks",
    "run_paper_session",
]
