"""Reusable paper-to-backtest parity harness.

This module is the paper-mode analog of the determinism harness and of the
Zig-core parity oracle: it proves that :func:`~harness.runtime.paper.run_paper_session`
and :func:`~harness.runtime.backtest.run_backtest` produce identical behavior
for the same deterministic event stream and configuration.  PaperShell is a
composition of the backtest engine capabilities, not a second engine, so any
kernel change that makes paper and backtest ledgers diverge is a defect; the
harness turns that property into a reusable, structured check that any test or
future CI gate can run.

One :class:`PaperParityScenario` carries a paper-mode :class:`TraderConfig`,
one deterministic quote/trade event stream, and explicit runtime knobs.  The
harness derives the equivalent :class:`BacktestConfig` from the trader
configuration, replays the identical stream through both engines —
``run_paper_session`` over a :class:`~harness.runtime.paper.ReplayPaperFeed`
and ``run_backtest`` over an explicit :class:`~harness.runtime.backtest.ReplayFixture`
carrying the same session window and streams — and compares the pinned
dimensions: replay data fingerprints, dispatch hash, admitted market events,
submitted orders, fills, fee assessments, portfolio snapshots, risk decisions,
and the strategy lifecycle.  :func:`run_paper_parity` returns a structured
:class:`PaperParityReport` naming every dimension and the first divergence;
:func:`assert_paper_parity` and :meth:`PaperParityReport.assert_clean` raise
:class:`PaperParityError` with a focused diff on divergence.

The abort policy is pinned to ``STOP`` on both sides so aborting scenarios
return comparable ``ABORTED`` results instead of raising, and the paper
session runs ``OPEN_ENDED`` over the scenario window so every event is
admitted exactly once on both sides.  Legitimate differences — wall-clock
session provenance, shell and engine identity, the paper-only session
declaration and recorder row counts, and the paper-only ``extra_latency`` knob
(pinned to zero so fill timestamps stay identical) — are documented in
:data:`EXCLUDED_DIMENSIONS` and never compared.  Everything else is pinned
byte-identical.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Final

from harness.configuration import (
    BacktestConfig,
    DataSnapshotConfig,
    TraderConfig,
    canonical_hash,
)
from harness.core.clock import Clock
from harness.core.types import QuoteEvent, TradeEvent
from harness.core.types.common import DecimalInput, exact_decimal
from harness.data import DEFAULT_HISTORY_CAPACITY, LakeReplayFeed, ReplayStream
from harness.fills import AssetClass as FeeAssetClass
from harness.fills import FeeSchedule
from harness.risk import RiskPipeline
from harness.runtime.backtest import (
    AbortPolicy,
    BacktestOptions,
    BacktestResult,
    BacktestStatus,
    FillModel,
    FillModelOptions,
    ReplayFixture,
    StrategyHaltPolicy,
    StrategyLoader,
    TeardownPolicy,
    run_backtest,
)
from harness.runtime.backtest.fees import DEFAULT_FEE_PRECISION
from harness.runtime.paper import (
    PaperOptions,
    PaperSessionPolicy,
    PaperSessionResult,
    PaperSessionStatus,
    ReplayPaperFeed,
    run_paper_session,
)
from lake.contracts import ExchangeSession

_ONE_DAY: Final = timedelta(days=1)
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class PaperParityError(AssertionError):
    """Raised when paper and backtest behavior diverges on a pinned dimension."""


class PaperParityConfigurationError(ValueError):
    """Raised when a parity scenario or comparison is misconfigured."""


class ParityDivergence(StrEnum):
    """Error taxonomy for one diverging pinned dimension."""

    DATA = "data_fingerprints"
    STATUS = "status"
    DISPATCH = "dispatch"
    MARKET_EVENTS = "market_events"
    ORDERS = "orders"
    FILLS = "fills"
    FEES = "fees"
    PORTFOLIO = "portfolio"
    RISK = "risk_decisions"
    LIFECYCLE = "strategy_lifecycle"


EXCLUDED_DIMENSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "provenance.booted_at_ended_at": (
            "PaperShell stamps session boot and shutdown provenance through "
            "the injected wall-clock adapter; the backtest engine has no wall "
            "clock. Parity runs inject a frozen clock, so provenance stays "
            "deterministic without being compared."
        ),
        "provenance.mode_and_engine_id": (
            "The paper result reports mode 'paper' and the backtest manifest "
            "reports shell 'backtest' with engine_id 'harness.backtest'; "
            "shell and engine identity are expected to differ."
        ),
        "provenance.session_declaration": (
            "session and session_policy are paper-only session-management "
            "fields; parity scenarios run OPEN_ENDED sessions over the "
            "shared event window and never compare them."
        ),
        "provenance.recorded_rows": (
            "recorded_rows reflect an optional paper-side LiveRecorder; "
            "parity runs record nothing and the backtest has no recorder."
        ),
        "policy.extra_latency": (
            "extra_latency is a paper-only venue latency knob; the harness "
            "pins it to zero so fill timestamps stay byte-identical."
        ),
        "policy.stop_and_at_close": (
            "STOPPED and AT_CLOSE paper statuses are session-policy outcomes "
            "with no backtest analog; parity scenarios run open-ended "
            "sessions, so only COMPLETED and ABORTED statuses are compared."
        ),
        "capability.strategy_scheduler": (
            "Paper strategy contexts currently receive the plain event-bus "
            "scheduler fallback while backtest contexts receive the refusing "
            "StrategyScheduler; strategies that schedule events can diverge "
            "and are out of scope for pinned parity."
        ),
        "capability.strategy_data_capability": (
            "Paper strategy contexts currently receive the raw HistoricalView "
            "while backtest contexts receive the opaque StrategyHistory "
            "wrapper; future reads raise LookaheadError through both, but "
            "data reads are not part of the pinned artifact comparison."
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ParityDimension:
    """One pinned comparison dimension and its first divergence, if any."""

    name: str
    matched: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        ParityDivergence(self.name)


def _validate_stream_order(
    symbol: str, stream: Sequence[QuoteEvent | TradeEvent]
) -> None:
    """Reject per-symbol streams that replay ordering cannot merge."""
    previous: tuple[datetime, int] | None = None
    for event in stream:
        key = (event.timestamp, event.sequence)
        if previous is not None and key < previous:
            raise PaperParityConfigurationError(
                f"events for {symbol!r} must be ordered by timestamp and "
                "sequence for deterministic replay"
            )
        previous = key


def _group_events(
    events: Sequence[QuoteEvent | TradeEvent],
) -> dict[str, list[QuoteEvent | TradeEvent]]:
    grouped: dict[str, list[QuoteEvent | TradeEvent]] = {}
    for event in events:
        grouped.setdefault(str(event.symbol), []).append(event)
    return grouped


def _event_stream_hash(events: Sequence[QuoteEvent | TradeEvent]) -> str:
    frames: list[dict[str, object]] = []
    for event in events:
        frame: dict[str, object] = {}
        for field in fields(event):
            frame[field.name] = getattr(event, field.name)
        frames.append(frame)
    return canonical_hash(frames)


@dataclass(frozen=True, slots=True)
class PaperParityScenario:
    """Complete deterministic input for one paper/backtest parity run.

    ``configuration`` must be a paper-mode :class:`TraderConfig`.  ``events``
    is the one deterministic quote/trade stream both engines replay; the
    harness derives the shared replay session from ``start`` (default: the
    earliest event timestamp) and ``end`` (default: one day after the latest
    event timestamp), so every event falls inside the pinned window.  The
    abort policy is pinned to ``STOP``, the session policy to ``OPEN_ENDED``,
    and ``extra_latency`` to zero.
    """

    configuration: TraderConfig
    events: Sequence[QuoteEvent | TradeEvent]
    run_id: str = "paper-parity"
    initial_cash: DecimalInput = Decimal("100000")
    fill_model: FillModel | str = FillModel.SIMPLE_TOUCH
    fill_options: FillModelOptions | None = None
    fee_precision: Decimal = DEFAULT_FEE_PRECISION
    asset_class: FeeAssetClass = FeeAssetClass.EQUITY
    history_capacity: int = DEFAULT_HISTORY_CAPACITY
    strategy_halt_policy: StrategyHaltPolicy = StrategyHaltPolicy.HALT_STRATEGY
    teardown_policy: TeardownPolicy = TeardownPolicy.ALWAYS
    start: datetime | None = None
    end: datetime | None = None
    session_date: date | None = None
    snapshot_id: str | None = None
    snapshot_fingerprint: str | None = None
    bar_interval: str = "1min"
    strategy_loader: StrategyLoader | None = None
    fee_schedule: FeeSchedule | None = None
    risk_pipeline: RiskPipeline | None = None
    wall_clock: Clock | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.configuration, TraderConfig):
            raise PaperParityConfigurationError("configuration must be a TraderConfig")
        if self.configuration.mode != "paper":
            raise PaperParityConfigurationError(
                "parity scenarios require a paper-mode trader configuration, "
                f"got mode {self.configuration.mode!r}"
            )
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise PaperParityConfigurationError("run_id must be a non-empty string")
        cash = exact_decimal(self.initial_cash, "initial_cash")
        if cash <= Decimal("0"):
            raise PaperParityConfigurationError("initial_cash must be positive")
        events = tuple(self.events)
        if not events:
            raise PaperParityConfigurationError("events must not be empty")
        if not all(isinstance(event, QuoteEvent | TradeEvent) for event in events):
            raise PaperParityConfigurationError(
                "events must contain QuoteEvent or TradeEvent values"
            )
        if any(event.timestamp.tzinfo is None for event in events):
            raise PaperParityConfigurationError(
                "event timestamps must be timezone-aware"
            )
        object.__setattr__(self, "events", events)
        start = self.start if self.start is not None else events[0].timestamp
        end = (
            self.end
            if self.end is not None
            else max(event.timestamp for event in events) + _ONE_DAY
        )
        if start.tzinfo is None or end.tzinfo is None:
            raise PaperParityConfigurationError(
                "scenario start and end must be timezone-aware"
            )
        if start >= end:
            raise PaperParityConfigurationError("scenario start must precede end")
        for event in events:
            if event.timestamp < start or event.timestamp >= end:
                raise PaperParityConfigurationError(
                    f"event {event.event_id!r} falls outside the pinned "
                    "[start, end) window"
                )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if self.session_date is None:
            object.__setattr__(self, "session_date", start.date())
        elif isinstance(self.session_date, datetime) or not isinstance(
            self.session_date, date
        ):
            raise PaperParityConfigurationError("session_date must be a calendar date")
        if self.snapshot_fingerprint is not None and (
            _SHA256.fullmatch(self.snapshot_fingerprint) is None
        ):
            raise PaperParityConfigurationError(
                "snapshot_fingerprint must be a lowercase 64-hex SHA-256 digest"
            )
        for symbol, stream in _group_events(events).items():
            _validate_stream_order(symbol, stream)
        self._validate_symbol_coverage()

    @property
    def effective_start(self) -> datetime:
        """Return the pinned window start."""
        assert self.start is not None
        return self.start

    @property
    def effective_end(self) -> datetime:
        """Return the pinned window end."""
        assert self.end is not None
        return self.end

    @property
    def scenario_hash(self) -> str:
        """Return the canonical hash of every pinned scenario input."""
        return canonical_hash(
            {
                "schema": "fca-paper-parity-scenario-v1",
                "run_id": self.run_id,
                "configuration_hash": canonical_hash(self.configuration),
                "initial_cash": exact_decimal(self.initial_cash, "initial_cash"),
                "fill_model": str(self.fill_model),
                "fee_precision": self.fee_precision,
                "asset_class": self.asset_class.value,
                "history_capacity": self.history_capacity,
                "strategy_halt_policy": self.strategy_halt_policy.value,
                "teardown_policy": self.teardown_policy.value,
                "start": self.effective_start,
                "end": self.effective_end,
                "session_date": self.session_date,
                "snapshot_id": self.snapshot_id,
                "snapshot_fingerprint": self.snapshot_fingerprint,
                "bar_interval": self.bar_interval,
                "event_fingerprint": _event_stream_hash(self.events),
            }
        )

    def run(self) -> PaperParityReport:
        """Run both engines over the same stream and return the parity report.

        Divergence is reported, never raised; use :meth:`assert_clean` or
        :func:`assert_paper_parity` to turn divergence into a test failure.
        """
        return run_paper_parity(self)

    def assert_clean(self) -> PaperParityReport:
        """Run the scenario and raise :class:`PaperParityError` on divergence."""
        return assert_paper_parity(self)

    def _validate_symbol_coverage(self) -> None:
        event_symbols = {str(event.symbol) for event in self.events}
        instance_symbols = {
            str(symbol)
            for instance in self.configuration.strategy_instances
            for symbol in instance.symbols
        }
        missing = sorted(instance_symbols - event_symbols)
        if missing:
            raise PaperParityConfigurationError(
                "scenario events must cover every configured strategy symbol: "
                + ", ".join(missing)
            )


class _FrozenClock:
    """Deterministic wall-clock adapter returning one fixed instant."""

    __slots__ = ("_moment",)

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


@dataclass(frozen=True, slots=True)
class _ReplayPlan:
    """The shared replay inputs handed to both engines."""

    sessions: tuple[ExchangeSession, ...]
    streams: tuple[ReplayStream, ...]
    event_symbols: tuple[str, ...]
    instance_symbols: tuple[str, ...]
    data_fingerprint: str
    snapshot_fingerprint: str

    @classmethod
    def build(cls, scenario: PaperParityScenario) -> _ReplayPlan:
        grouped = _group_events(scenario.events)
        streams = tuple(
            ReplayStream(symbol, None, grouped[symbol]) for symbol in sorted(grouped)
        )
        session_date = scenario.session_date
        assert session_date is not None
        sessions = (
            ExchangeSession(
                session_date,
                scenario.effective_start,
                scenario.effective_end,
            ),
        )
        probe = LakeReplayFeed(sessions, streams=streams)
        materialized = tuple(probe)
        if len(materialized) != len(scenario.events):
            raise PaperParityConfigurationError(
                "the pinned replay session window dropped scenario events"
            )
        instance_symbols = sorted(
            {
                str(symbol)
                for instance in scenario.configuration.strategy_instances
                for symbol in instance.symbols
            }
        )
        data_fingerprint = probe.fingerprints.data
        return _ReplayPlan(
            sessions=sessions,
            streams=streams,
            event_symbols=tuple(sorted(grouped)),
            instance_symbols=tuple(instance_symbols),
            data_fingerprint=data_fingerprint,
            snapshot_fingerprint=(
                scenario.snapshot_fingerprint
                if scenario.snapshot_fingerprint is not None
                else data_fingerprint
            ),
        )

    def paper_options(self, scenario: PaperParityScenario) -> PaperOptions:
        return PaperOptions(
            run_id=scenario.run_id,
            initial_cash=scenario.initial_cash,
            fill_model=scenario.fill_model,
            fill_options=scenario.fill_options,
            fee_precision=scenario.fee_precision,
            asset_class=scenario.asset_class,
            history_capacity=scenario.history_capacity,
            abort_policy=AbortPolicy.STOP,
            strategy_halt_policy=scenario.strategy_halt_policy,
            teardown_policy=scenario.teardown_policy,
            extra_latency=timedelta(0),
            session_policy=PaperSessionPolicy.OPEN_ENDED,
            start=scenario.effective_start,
        )

    def backtest_options(self, scenario: PaperParityScenario) -> BacktestOptions:
        return BacktestOptions(
            fill_model=scenario.fill_model,
            fill_options=scenario.fill_options,
            fee_precision=scenario.fee_precision,
            asset_class=scenario.asset_class,
            history_capacity=scenario.history_capacity,
            abort_policy=AbortPolicy.STOP,
            strategy_halt_policy=scenario.strategy_halt_policy,
            teardown_policy=scenario.teardown_policy,
        )

    def fixture(self) -> ReplayFixture:
        return ReplayFixture(
            sessions=self.sessions,
            streams=self.streams,
            source_id="paper-parity-streams",
            symbols=self.event_symbols,
            expected_data_fingerprint=self.snapshot_fingerprint,
        )

    def backtest_configuration(self, scenario: PaperParityScenario) -> BacktestConfig:
        trader = scenario.configuration
        deployment = trader.deployment.model_copy(
            update={"network_access": "disabled", "replicas": 1}
        )
        return BacktestConfig(
            schema_version=1,
            shell="backtest",
            run_id=scenario.run_id,
            start=scenario.effective_start,
            end=scenario.effective_end,
            clock="event_time",
            event_ordering="timestamp_then_sequence_then_class_then_feed",
            seed=trader.seed,
            base_currency=trader.base_currency,
            initial_cash=exact_decimal(scenario.initial_cash, "initial_cash"),
            data=DataSnapshotConfig(
                snapshot_id=(
                    scenario.snapshot_id
                    if scenario.snapshot_id is not None
                    else f"paper-parity-{scenario.run_id}"
                ),
                fingerprint=self.snapshot_fingerprint,
                symbols=self.instance_symbols,
                bar_interval=scenario.bar_interval,
            ),
            strategy_instances=trader.strategy_instances,
            risk_pipeline=trader.risk_pipeline,
            fees=trader.fees,
            deployment=deployment,
        )


@dataclass(frozen=True, slots=True)
class PaperParityReport:
    """Structured parity outcome naming every pinned dimension."""

    run_id: str
    scenario_hash: str | None
    dimensions: tuple[ParityDimension, ...]
    paper: PaperSessionResult
    backtest: BacktestResult

    @property
    def parity_clean(self) -> bool:
        """Return whether every pinned dimension matched."""
        return all(dimension.matched for dimension in self.dimensions)

    @property
    def divergences(self) -> tuple[ParityDivergence, ...]:
        """Return every diverging dimension in canonical order."""
        return tuple(
            ParityDivergence(dimension.name)
            for dimension in self.dimensions
            if not dimension.matched
        )

    @property
    def first_divergence(self) -> ParityDivergence | None:
        """Return the first diverging dimension, or ``None`` when clean."""
        for dimension in self.dimensions:
            if not dimension.matched:
                return ParityDivergence(dimension.name)
        return None

    @property
    def excluded_dimensions(self) -> Mapping[str, str]:
        """Return the documented legitimate differences that are not compared."""
        return EXCLUDED_DIMENSIONS

    def summary(self) -> str:
        """Return a one-line human-readable parity summary."""
        if self.parity_clean:
            return (
                f"paper/backtest parity clean ({self.run_id}): "
                f"{len(self.dimensions)} pinned dimensions matched"
            )
        failures = "; ".join(
            f"{dimension.name}: {dimension.detail}"
            for dimension in self.dimensions
            if not dimension.matched
        )
        return f"paper/backtest parity diverged ({self.run_id}): {failures}"

    def assert_clean(self) -> PaperParityReport:
        """Raise :class:`PaperParityError` when any pinned dimension diverges."""
        if not self.parity_clean:
            raise PaperParityError(self.summary())
        return self

    def artifacts(self) -> dict[str, object]:
        """Return the canonical, JSON-ready parity report."""
        return {
            "schema": "fca-paper-parity-report-v1",
            "run_id": self.run_id,
            "scenario_hash": self.scenario_hash,
            "parity_clean": self.parity_clean,
            "first_divergence": (
                None if self.first_divergence is None else self.first_divergence.value
            ),
            "divergences": [divergence.value for divergence in self.divergences],
            "dimensions": [
                {
                    "name": dimension.name,
                    "matched": dimension.matched,
                    "detail": dimension.detail,
                }
                for dimension in self.dimensions
            ],
            "excluded_dimensions": dict(self.excluded_dimensions),
            "paper": _engine_summary(
                status=self.paper.status.value,
                reason=self.paper.reason,
                dispatch_hash=self.paper.dispatch_hash,
                dispatched_count=self.paper.dispatched_count,
                market_event_count=self.paper.market_event_count,
                order_count=self.paper.order_count,
                fill_count=len(self.paper.fills),
                risk_decision_count=len(self.paper.risk_decisions),
            ),
            "backtest": _engine_summary(
                status=self.backtest.status.value,
                reason=self.backtest.reason,
                dispatch_hash=self.backtest.dispatch_hash,
                dispatched_count=self.backtest.dispatched_count,
                market_event_count=self.backtest.market_event_count,
                order_count=self.backtest.order_count,
                fill_count=len(self.backtest.fills),
                risk_decision_count=len(self.backtest.risk_decisions),
            ),
        }


def _engine_summary(
    *,
    status: str,
    reason: str | None,
    dispatch_hash: str,
    dispatched_count: int,
    market_event_count: int,
    order_count: int,
    fill_count: int,
    risk_decision_count: int,
) -> dict[str, object]:
    return {
        "status": status,
        "reason": reason,
        "dispatch_hash": dispatch_hash,
        "counts": {
            "dispatched": dispatched_count,
            "market_events": market_event_count,
            "orders": order_count,
            "fills": fill_count,
            "risk_decisions": risk_decision_count,
        },
    }


def run_paper_parity(scenario: PaperParityScenario) -> PaperParityReport:
    """Run both engines over the scenario stream and return the parity report."""
    if not isinstance(scenario, PaperParityScenario):
        raise TypeError("scenario must be a PaperParityScenario")
    plan = _ReplayPlan.build(scenario)
    paper = run_paper_session(
        scenario.configuration,
        ReplayPaperFeed(LakeReplayFeed(plan.sessions, streams=plan.streams)),
        options=plan.paper_options(scenario),
        fee_schedule=scenario.fee_schedule,
        risk_pipeline=scenario.risk_pipeline,
        strategy_loader=scenario.strategy_loader,
        recorder=None,
        wall_clock=(
            scenario.wall_clock
            if scenario.wall_clock is not None
            else _FrozenClock(scenario.effective_start)
        ),
    )
    backtest = run_backtest(
        plan.backtest_configuration(scenario),
        data_source=plan.fixture(),
        strategy_loader=scenario.strategy_loader,
        fee_schedule=scenario.fee_schedule,
        risk_pipeline=scenario.risk_pipeline,
        options=plan.backtest_options(scenario),
    )
    return compare_paper_backtest(paper, backtest, scenario_hash=scenario.scenario_hash)


def assert_paper_parity(scenario: PaperParityScenario) -> PaperParityReport:
    """Run the scenario and raise :class:`PaperParityError` on divergence."""
    return run_paper_parity(scenario).assert_clean()


def compare_paper_backtest(
    paper: PaperSessionResult,
    backtest: BacktestResult,
    *,
    scenario_hash: str | None = None,
) -> PaperParityReport:
    """Compare one paper session result against one backtest result."""
    if not isinstance(paper, PaperSessionResult):
        raise TypeError("paper must be a PaperSessionResult")
    if not isinstance(backtest, BacktestResult):
        raise TypeError("backtest must be a BacktestResult")
    if paper.run_id != backtest.configuration.run_id:
        raise PaperParityConfigurationError(
            f"run_id mismatch: paper {paper.run_id!r} vs backtest "
            f"{backtest.configuration.run_id!r}"
        )
    return PaperParityReport(
        run_id=paper.run_id,
        scenario_hash=scenario_hash,
        dimensions=(
            _data_dimension(paper, backtest),
            _status_dimension(paper, backtest),
            _dispatch_dimension(paper, backtest),
            _count_dimension(
                ParityDivergence.MARKET_EVENTS,
                paper.market_event_count,
                backtest.market_event_count,
            ),
            _count_dimension(
                ParityDivergence.ORDERS, paper.order_count, backtest.order_count
            ),
            _difference_dimension(
                ParityDivergence.FILLS, paper.fills, backtest.fills, "fills"
            ),
            _fees_dimension(paper, backtest),
            _difference_dimension(
                ParityDivergence.PORTFOLIO,
                paper.portfolio,
                backtest.portfolio,
                "portfolio",
            ),
            _difference_dimension(
                ParityDivergence.RISK,
                paper.risk_decisions,
                backtest.risk_decisions,
                "risk_decisions",
            ),
            _lifecycle_dimension(paper, backtest),
        ),
        paper=paper,
        backtest=backtest,
    )


def _data_dimension(
    paper: PaperSessionResult, backtest: BacktestResult
) -> ParityDimension:
    paper_fingerprints = paper.feed_fingerprints
    if paper_fingerprints is None:
        return ParityDimension(
            ParityDivergence.DATA.value,
            False,
            "paper result carries no replay fingerprints",
        )
    preflight_fingerprints = backtest.preflight.fingerprints
    if paper_fingerprints.file != preflight_fingerprints.file:
        return ParityDimension(
            ParityDivergence.DATA.value,
            False,
            "file fingerprint differs",
        )
    if paper.status is not PaperSessionStatus.COMPLETED:
        # An incomplete session stops its replay boundary before exhaustion, so
        # the paper-side row/data fingerprints reflect only the consumed
        # prefix; they are boundary provenance, not parity evidence.
        return ParityDimension(ParityDivergence.DATA.value, True, None)
    for label, paper_value, backtest_value in (
        ("row", paper_fingerprints.row, preflight_fingerprints.row),
        ("data", paper_fingerprints.data, preflight_fingerprints.data),
    ):
        if paper_value != backtest_value:
            return ParityDimension(
                ParityDivergence.DATA.value,
                False,
                f"{label} fingerprint differs",
            )
    return ParityDimension(ParityDivergence.DATA.value, True, None)


def _status_dimension(
    paper: PaperSessionResult, backtest: BacktestResult
) -> ParityDimension:
    name = ParityDivergence.STATUS.value
    if paper.status is PaperSessionStatus.COMPLETED:
        if backtest.status is not BacktestStatus.COMPLETED:
            return ParityDimension(
                name,
                False,
                f"paper completed but backtest status is {backtest.status.value}",
            )
        if paper.reason is not None or backtest.reason is not None:
            return ParityDimension(
                name, False, f"reason {paper.reason!r} != {backtest.reason!r}"
            )
        return ParityDimension(name, True, None)
    if paper.status is PaperSessionStatus.ABORTED:
        if backtest.status is not BacktestStatus.ABORTED:
            return ParityDimension(
                name,
                False,
                f"paper aborted but backtest status is {backtest.status.value}",
            )
        if paper.reason != backtest.reason:
            return ParityDimension(
                name, False, f"reason {paper.reason!r} != {backtest.reason!r}"
            )
        return ParityDimension(name, True, None)
    return ParityDimension(
        name,
        False,
        f"paper status {paper.status.value} is session-policy provenance "
        "with no backtest analog",
    )


def _dispatch_dimension(
    paper: PaperSessionResult, backtest: BacktestResult
) -> ParityDimension:
    name = ParityDivergence.DISPATCH.value
    if paper.dispatch_hash != backtest.dispatch_hash:
        return ParityDimension(
            name,
            False,
            f"dispatch hash {paper.dispatch_hash} != {backtest.dispatch_hash}",
        )
    if paper.dispatched_count != backtest.dispatched_count:
        return ParityDimension(
            name,
            False,
            f"dispatched count {paper.dispatched_count} != {backtest.dispatched_count}",
        )
    return ParityDimension(name, True, None)


def _count_dimension(
    name: ParityDivergence, paper: int, backtest: int
) -> ParityDimension:
    if paper != backtest:
        return ParityDimension(name.value, False, f"{paper} != {backtest}")
    return ParityDimension(name.value, True, None)


def _difference_dimension(
    name: ParityDivergence, paper: object, backtest: object, path: str
) -> ParityDimension:
    detail = _first_difference(paper, backtest, path)
    return ParityDimension(name.value, detail is None, detail)


def _fees_dimension(
    paper: PaperSessionResult, backtest: BacktestResult
) -> ParityDimension:
    detail = _first_difference(
        paper.fee_assessments, backtest.fee_assessments, "fee_assessments"
    )
    if detail is None:
        detail = _first_difference(paper.fee_totals, backtest.fee_totals, "fee_totals")
    return ParityDimension(ParityDivergence.FEES.value, detail is None, detail)


def _lifecycle_dimension(
    paper: PaperSessionResult, backtest: BacktestResult
) -> ParityDimension:
    name = ParityDivergence.LIFECYCLE.value
    for label, paper_value, backtest_value in (
        ("halted", paper.halted_strategies, backtest.halted_strategies),
        ("stopped", paper.stopped_strategies, backtest.stopped_strategies),
        ("teardown_errors", paper.teardown_errors, backtest.teardown_errors),
        (
            "lookahead_violations",
            paper.lookahead_violations,
            backtest.lookahead_violations,
        ),
    ):
        if paper_value != backtest_value:
            return ParityDimension(
                name,
                False,
                f"{label} {paper_value} != {backtest_value}",
            )
    return ParityDimension(name, True, None)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _leaf_difference(left: object, right: object, path: str) -> str | None:
    if isinstance(left, Enum) and isinstance(right, Enum):
        if type(left) is type(right) and left.value == right.value:
            return None
        return f"{path}: {left.value} != {right.value}"
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        return None if str(left) == str(right) else f"{path}: {left} != {right}"
    if isinstance(left, datetime) and isinstance(right, datetime):
        return (
            None
            if _iso(left) == _iso(right)
            else f"{path}: {_iso(left)} != {_iso(right)}"
        )
    if left == right:
        return None
    return f"{path}: {left!r} != {right!r}"


def _first_difference(left: object, right: object, path: str = "value") -> str | None:
    """Return a focused description of the first byte-level difference."""
    if left is right:
        return None
    if (
        is_dataclass(left)
        and is_dataclass(right)
        and not (isinstance(left, type) or isinstance(right, type))
    ):
        if type(left) is not type(right):
            return f"{path}: {type(left).__name__} != {type(right).__name__}"
        for field in fields(left):
            difference = _first_difference(
                getattr(left, field.name),
                getattr(right, field.name),
                f"{path}.{field.name}",
            )
            if difference is not None:
                return difference
        return None
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        paper_keys = sorted(left, key=str)
        backtest_keys = sorted(right, key=str)
        if paper_keys != backtest_keys:
            return f"{path}: keys {paper_keys} != {backtest_keys}"
        for key in paper_keys:
            difference = _first_difference(left[key], right[key], f"{path}[{key!r}]")
            if difference is not None:
                return difference
        return None
    if (
        isinstance(left, Sequence)
        and isinstance(right, Sequence)
        and not isinstance(left, str | bytes | bytearray)
        and not isinstance(right, str | bytes | bytearray)
    ):
        if len(left) != len(right):
            return f"{path}: length {len(left)} != {len(right)}"
        for index, (paper_value, backtest_value) in enumerate(
            zip(left, right, strict=True)
        ):
            difference = _first_difference(
                paper_value, backtest_value, f"{path}[{index}]"
            )
            if difference is not None:
                return difference
        return None
    return _leaf_difference(left, right, path)


__all__ = [
    "EXCLUDED_DIMENSIONS",
    "PaperParityConfigurationError",
    "PaperParityError",
    "PaperParityReport",
    "PaperParityScenario",
    "ParityDimension",
    "ParityDivergence",
    "assert_paper_parity",
    "compare_paper_backtest",
    "run_paper_parity",
]
