"""ShadowShell: live market data with observe-only broker routing.

Shadow mode is the rehearsal runtime between paper and live: the strategy
fleet consumes the real live market-data boundary while every broker command
is captured by an observe-only sink that never transmits, and the same events
drive the shared simulated venue side-by-side so each broker intent acquires
a would-be simulated outcome.  The resulting :class:`ShadowReport` compares
every broker intent and every simulated outcome against the live execution
prints supplied by the deployment boundary, so a shadow session is a complete,
deterministic rehearsal artifact: every order the fleet *would* have routed,
every fill the simulator *would* have produced, and every place the live
world disagreed with either.

Shadow is a composition, not a second engine: the kernel, simulated venue,
fill plugins, fee schedule, risk pipeline, and virtual books are exactly the
:class:`~harness.runtime.paper.PaperShell` capabilities that produce paper
and backtest ledgers.  The only injected difference is the risk pipeline
wrapper that records every risk-approved order as a :class:`BrokerIntent`
on :class:`ObserveOnlyBrokerSink` — a pass-through for decisions, so shadow
sessions stay ledger-identical to the same paper session.  A shadow-mode
:class:`~harness.configuration.TraderConfig` drives the inner shell through
its paper mode; the deployment mode is preserved on the report.

The synchronous kernel surface stays deterministic: no wall clock is read
(wall-clock provenance remains the injected adapter boundary's job), no
randomness is drawn, and no network client exists anywhere in this module —
the observe-only sink is structural proof that no order leaves the runtime.
Live prints enter only as explicit, event-time-stamped records, and every
comparison is computed from event-time data alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from harness.configuration import TraderConfig
from harness.core.clock import Clock
from harness.core.types import Fill, OrderRequest, Side, SingleLegOrder
from harness.core.types.common import DecimalInput, exact_decimal, text
from harness.fills import FeeSchedule
from harness.portfolio import PortfolioSnapshot
from harness.risk import RiskPipeline
from harness.risk.models import (
    RiskContext,
    RiskDecision,
)
from harness.runtime.backtest.manifest import _fill_json
from harness.runtime.backtest.strategies import StrategyLoader
from harness.runtime.paper import (
    PaperFeed,
    PaperOptions,
    PaperSessionResult,
    PaperShell,
)

_REPORT_SCHEMA: Final = "fca-shadow-report-v1"


class ShadowError(RuntimeError):
    """Base class for every shadow runtime error."""


class ShadowConfigurationError(ShadowError, ValueError):
    """Raised when the supplied configuration cannot drive a shadow session."""


class ShadowVerdict(StrEnum):
    """Comparison verdict between one broker intent's would-be and live outcome.

    ``MATCHED`` and ``DIVERGED`` are the two comparable verdicts: both sides
    exist and agree, or both sides exist and disagree on a pinned field.
    ``WOULD_BE_ONLY`` records a simulated fill with no live print available,
    ``UNFILLED`` records an intent the simulator never filled, and
    ``LIVE_ONLY`` records a live print with no matching broker intent — the
    one verdict that always means the live world did something shadow did
    not intend.
    """

    MATCHED = "matched"
    DIVERGED = "diverged"
    WOULD_BE_ONLY = "would_be_only"
    UNFILLED = "unfilled"
    LIVE_ONLY = "live_only"


# --------------------------------------------------------------------------
# Observe-only broker boundary
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrokerIntent:
    """One order the fleet would have routed to the live broker.

    Intents are recorded at risk-pipeline decision time, so the trail
    contains exactly the orders admission approved and nothing else.  The
    ``sequence`` is a 1-based ordinal in the owning sink's trail; no
    timestamp is carried because the synchronous kernel has no wall clock.
    """

    sequence: int
    order_id: str
    strategy_id: str | None
    order: SingleLegOrder

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("broker intent sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("broker intent sequence must be positive")
        if not isinstance(self.order, SingleLegOrder):
            raise ValueError("broker intents record single-leg orders only")
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        if self.strategy_id is not None:
            object.__setattr__(
                self, "strategy_id", text(self.strategy_id, "strategy_id")
            )


class ObserveOnlyBrokerSink:
    """The observe-only live-broker command boundary for shadow sessions.

    The sink is the structural observe-only guarantee: it accepts risk-
    approved order intents, records each one immutably with a 1-based
    sequence, and offers no transmission path of any kind — no network
    client, no adapter, no queue, and no wall clock.  A shadow session can
    therefore never leak an order to a real broker, and the recorded trail
    is the complete set of commands a live deployment would have issued.
    """

    __slots__ = ("_intents",)

    def __init__(self) -> None:
        self._intents: tuple[BrokerIntent, ...] = ()

    @property
    def intents(self) -> tuple[BrokerIntent, ...]:
        """Return the complete immutable intent trail in record order."""
        return self._intents

    def __len__(self) -> int:
        """Return how many intents the sink has recorded."""
        return len(self._intents)

    def submit(
        self, order: OrderRequest, *, strategy_id: str | None = None
    ) -> BrokerIntent:
        """Record one order intent without transmitting anything.

        Only routable single-leg orders are recorded: they are exactly the
        orders the simulated venue can produce would-be outcomes for.
        Multi-leg orders are refused so the trail can never overstate what
        a live broker would have received.
        """
        if not isinstance(order, SingleLegOrder):
            raise ShadowConfigurationError(
                "the observe-only sink records single-leg order intents only"
            )
        if strategy_id is not None:
            strategy_id = text(strategy_id, "strategy_id")
        intent = BrokerIntent(
            sequence=len(self._intents) + 1,
            order_id=order.order_id,
            strategy_id=strategy_id,
            order=order,
        )
        self._intents = (*self._intents, intent)
        return intent


class _IntentRecordingPipeline(RiskPipeline):
    """Risk pipeline pass-through that routes approved orders to the sink.

    The wrapper delegates every evaluation to the composed pipeline and
    records each approved single-leg order on the observe-only sink at
    decision time — the exact point a live shell would hand the order to
    its broker client.  Decisions are returned untouched, so shadow
    sessions stay ledger-identical to the same paper session.
    """

    __slots__ = ("_sink",)

    def __init__(self, delegate: RiskPipeline, sink: ObserveOnlyBrokerSink) -> None:
        super().__init__(delegate.rules, on_reject=delegate.on_reject)
        self._sink = sink

    def evaluate(
        self,
        order: OrderRequest,
        context: RiskContext | PortfolioSnapshot,
        *,
        prices: Mapping[str, Decimal] | None = None,
        daily_loss: DecimalInput | None = None,
        order_count: int | None = None,
        strategy_id: str | None = None,
    ) -> RiskDecision:
        decision = super().evaluate(
            order,
            context,
            prices=prices,
            daily_loss=daily_loss,
            order_count=order_count,
            strategy_id=strategy_id,
        )
        if decision.approved and isinstance(decision.order, SingleLegOrder):
            self._sink.submit(decision.order, strategy_id=decision.strategy_id)
        return decision


# --------------------------------------------------------------------------
# Live prints and comparison records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LivePrint:
    """One live execution print supplied by the deployment boundary.

    The record carries event time only: ``timestamp`` is the exchange
    moment of the live execution, never a wall-clock receipt time, so
    comparisons stay deterministic.  Live prints are the deployment
    boundary's responsibility to capture and supply; the shadow runtime
    never fetches them.
    """

    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    timestamp: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        object.__setattr__(self, "symbol", text(self.symbol, "symbol"))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "quantity", exact_decimal(self.quantity, "quantity"))
        if self.quantity <= 0:
            raise ValueError("live print quantity must be positive")
        object.__setattr__(self, "price", exact_decimal(self.price, "price"))
        if self.price <= 0:
            raise ValueError("live print price must be positive")
        if self.timestamp.tzinfo is None:
            raise ValueError("live print timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class IntentComparison:
    """One broker intent compared against its would-be and live outcomes."""

    intent_sequence: int
    order_id: str
    verdict: ShadowVerdict
    would_be: tuple[Fill, ...]
    live: LivePrint | None
    detail: str | None

    def __post_init__(self) -> None:
        if isinstance(self.intent_sequence, bool) or not isinstance(
            self.intent_sequence, int
        ):
            raise ValueError("comparison intent_sequence must be an integer")
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        object.__setattr__(self, "verdict", ShadowVerdict(self.verdict))
        object.__setattr__(self, "would_be", tuple(self.would_be))
        if self.detail is not None:
            object.__setattr__(self, "detail", text(self.detail, "detail"))


# --------------------------------------------------------------------------
# Shadow report
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """The complete deterministic rehearsal artifact of one shadow session.

    ``intents`` is the observe-only sink's trail; ``comparisons`` carries
    exactly one row per intent; ``live_only_prints`` carries live prints no
    intent explains.  :attr:`parity_clean` is the rehearsal verdict: no
    comparable pair diverged and no live print went unexplained.  Intents
    the simulator never filled and fills without live prints are recorded
    informationally — they are simulation outcomes, not contradictions.
    """

    run_id: str
    deployment_mode: str
    session: PaperSessionResult
    intents: tuple[BrokerIntent, ...]
    comparisons: tuple[IntentComparison, ...]
    live_only_prints: tuple[LivePrint, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", text(self.run_id, "run_id"))
        object.__setattr__(
            self, "deployment_mode", text(self.deployment_mode, "deployment_mode")
        )
        object.__setattr__(self, "intents", tuple(self.intents))
        object.__setattr__(self, "comparisons", tuple(self.comparisons))
        object.__setattr__(self, "live_only_prints", tuple(self.live_only_prints))

    @property
    def parity_clean(self) -> bool:
        """Return whether live outcomes agree with would-be outcomes."""
        return not self.live_only_prints and not any(
            comparison.verdict is ShadowVerdict.DIVERGED
            for comparison in self.comparisons
        )

    @property
    def matched_count(self) -> int:
        """Return how many intents matched their live print exactly."""
        return self._count(ShadowVerdict.MATCHED)

    @property
    def diverged_count(self) -> int:
        """Return how many comparable pairs diverged."""
        return self._count(ShadowVerdict.DIVERGED)

    @property
    def would_be_only_count(self) -> int:
        """Return how many intents have a simulated fill and no live print."""
        return self._count(ShadowVerdict.WOULD_BE_ONLY)

    @property
    def unfilled_count(self) -> int:
        """Return how many intents the simulator never filled."""
        return self._count(ShadowVerdict.UNFILLED)

    @property
    def live_only_count(self) -> int:
        """Return how many live prints no intent explains."""
        return len(self.live_only_prints)

    def _count(self, verdict: ShadowVerdict) -> int:
        return sum(
            1 for comparison in self.comparisons if comparison.verdict is verdict
        )

    def summary(self) -> str:
        """Return a one-line human-readable shadow rehearsal summary."""
        if self.parity_clean:
            return (
                f"shadow parity clean ({self.run_id}): {len(self.intents)} "
                f"intents, {self.matched_count} matched, "
                f"{self.would_be_only_count} without live prints, "
                f"{self.unfilled_count} unfilled"
            )
        failures = [
            comparison.detail
            for comparison in self.comparisons
            if comparison.verdict is ShadowVerdict.DIVERGED
            and comparison.detail is not None
        ]
        if self.live_only_prints:
            failures.append(
                f"{self.live_only_count} live prints without broker intents"
            )
        return f"shadow parity diverged ({self.run_id}): {'; '.join(failures)}"

    def artifacts(self) -> dict[str, object]:
        """Return the canonical, JSON-ready shadow report.

        Wall-clock provenance (session boot and shutdown stamps) is
        deliberately excluded so identical event streams and live prints
        produce byte-identical artifacts.
        """
        return {
            "schema": _REPORT_SCHEMA,
            "run_id": self.run_id,
            "deployment_mode": self.deployment_mode,
            "parity_clean": self.parity_clean,
            "session": {
                "status": self.session.status.value,
                "reason": self.session.reason,
                "dispatch_hash": self.session.dispatch_hash,
                "dispatched_count": self.session.dispatched_count,
                "market_event_count": self.session.market_event_count,
                "order_count": self.session.order_count,
                "last_event_time": (
                    None
                    if self.session.last_event_time is None
                    else self.session.last_event_time.isoformat(timespec="microseconds")
                ),
            },
            "counts": {
                "intents": len(self.intents),
                "matched": self.matched_count,
                "diverged": self.diverged_count,
                "would_be_only": self.would_be_only_count,
                "unfilled": self.unfilled_count,
                "live_only": self.live_only_count,
            },
            "intents": [_intent_json(intent) for intent in self.intents],
            "would_be_fills": [_fill_json(fill) for fill in self.session.fills],
            "comparisons": [
                _comparison_json(comparison) for comparison in self.comparisons
            ],
            "live_only_prints": [
                _print_json(print_) for print_ in self.live_only_prints
            ],
        }


def _intent_json(intent: BrokerIntent) -> dict[str, object]:
    return {
        "sequence": intent.sequence,
        "order_id": intent.order_id,
        "strategy_id": intent.strategy_id,
        "symbol": str(intent.order.symbol),
        "side": intent.order.side.value,
        "quantity": str(intent.order.quantity),
        "order_type": intent.order.order_type.value,
    }


def _comparison_json(comparison: IntentComparison) -> dict[str, object]:
    return {
        "intent_sequence": comparison.intent_sequence,
        "order_id": comparison.order_id,
        "verdict": comparison.verdict.value,
        "detail": comparison.detail,
        "would_be_fill_ids": [fill.fill_id for fill in comparison.would_be],
        "live": None if comparison.live is None else _print_json(comparison.live),
    }


def _print_json(print_: LivePrint) -> dict[str, object]:
    return {
        "order_id": print_.order_id,
        "symbol": print_.symbol,
        "side": print_.side.value,
        "quantity": str(print_.quantity),
        "price": str(print_.price),
        "timestamp": print_.timestamp.isoformat(timespec="microseconds"),
    }


# --------------------------------------------------------------------------
# Shadow shell
# --------------------------------------------------------------------------


class ShadowShell:
    """Compose the paper runtime into one observe-only shadow session.

    The shell binds every configured strategy to its own virtual book inside
    a :class:`~harness.runtime.paper.PaperShell` driven by the live feed,
    wraps the pre-trade risk pipeline so every approved order is recorded on
    the :class:`ObserveOnlyBrokerSink` instead of any broker client, and
    compares the simulated venue's would-be fills against the live prints
    supplied at report time.  Shadow-mode trader configurations drive the
    inner shell through their paper mode; the deployment mode is preserved
    on the report.
    """

    __slots__ = (
        "_deployment_mode",
        "_run_id",
        "_shell",
        "_sink",
    )

    def __init__(
        self,
        configuration: TraderConfig,
        *,
        options: PaperOptions,
        sink: ObserveOnlyBrokerSink | None = None,
        strategy_loader: StrategyLoader | None = None,
        fee_schedule: FeeSchedule | None = None,
        risk_pipeline: RiskPipeline | None = None,
        wall_clock: Clock | None = None,
    ) -> None:
        if not isinstance(configuration, TraderConfig):
            raise ShadowConfigurationError("configuration must be a TraderConfig")
        if configuration.mode not in ("paper", "shadow"):
            raise ShadowConfigurationError(
                "ShadowShell requires a paper or shadow trader configuration, "
                f"got mode {configuration.mode!r}"
            )
        if not isinstance(options, PaperOptions):
            raise ShadowConfigurationError("options must be a PaperOptions")
        self._deployment_mode = configuration.mode
        self._run_id = text(options.run_id, "run_id")
        self._sink = ObserveOnlyBrokerSink() if sink is None else sink
        if not isinstance(self._sink, ObserveOnlyBrokerSink):
            raise ShadowConfigurationError("sink must be an ObserveOnlyBrokerSink")
        base = (
            risk_pipeline
            if risk_pipeline is not None
            else RiskPipeline(configuration.risk_pipeline)
        )
        if not isinstance(base, RiskPipeline):
            raise ShadowConfigurationError("risk_pipeline must be a RiskPipeline")
        paper_configuration = (
            configuration
            if configuration.mode == "paper"
            else configuration.model_copy(update={"mode": "paper"})
        )
        self._shell = PaperShell(
            paper_configuration,
            options=options,
            fee_schedule=fee_schedule,
            risk_pipeline=_IntentRecordingPipeline(base, self._sink),
            strategy_loader=strategy_loader,
            recorder=None,
            wall_clock=wall_clock,
        )

    @property
    def run_id(self) -> str:
        """Return the declared session run identifier."""
        return self._run_id

    @property
    def deployment_mode(self) -> str:
        """Return the trader configuration mode this shell was built from."""
        return self._deployment_mode

    @property
    def sink(self) -> ObserveOnlyBrokerSink:
        """Return the observe-only broker sink collecting intents."""
        return self._sink

    @property
    def running(self) -> bool:
        """Return whether :meth:`run` is currently driving the kernel."""
        return self._shell.running

    @property
    def finalized(self) -> bool:
        """Return whether the session has been torn down and finalized."""
        return self._shell.finalized

    def stop(self, reason: str | None = None) -> None:
        """Request a clean shutdown at the end of the current unit of work."""
        self._shell.stop(reason)

    def run(
        self,
        feed: PaperFeed,
        *,
        live_prints: Sequence[LivePrint] = (),
    ) -> ShadowReport:
        """Drive the session from ``feed`` and return the shadow report.

        The feed is any :class:`~harness.runtime.paper.PaperFeed` boundary;
        live boundaries never exhaust, so a live session runs until
        :meth:`stop` is requested or its session policy closes it.  The
        report is built after finalization from the sink trail, the session
        result, and the supplied live prints.
        """
        if not isinstance(feed, PaperFeed):
            raise ShadowConfigurationError("feed must implement PaperFeed")
        prints = _validated_prints(live_prints)
        result = self._shell.run(feed)
        return _build_report(
            run_id=self._run_id,
            deployment_mode=self._deployment_mode,
            session=result,
            intents=self._sink.intents,
            prints=prints,
        )

    def result(self) -> ShadowReport:
        """Return the shadow report, building an in-flight snapshot if needed."""
        return _build_report(
            run_id=self._run_id,
            deployment_mode=self._deployment_mode,
            session=self._shell.result(),
            intents=self._sink.intents,
            prints={},
        )

    def close(self) -> ShadowReport:
        """Stop, tear down, and finalize the session; idempotent."""
        result = self._shell.close()
        return _build_report(
            run_id=self._run_id,
            deployment_mode=self._deployment_mode,
            session=result,
            intents=self._sink.intents,
            prints={},
        )


def _validated_prints(
    live_prints: Sequence[LivePrint],
) -> Mapping[str, LivePrint]:
    prints: dict[str, LivePrint] = {}
    for candidate in live_prints:
        if not isinstance(candidate, LivePrint):
            raise ShadowConfigurationError("live prints must be LivePrint records")
        if candidate.order_id in prints:
            raise ShadowConfigurationError(
                f"duplicate live print for order id {candidate.order_id!r}"
            )
        prints[candidate.order_id] = candidate
    return prints


def _build_report(
    *,
    run_id: str,
    deployment_mode: str,
    session: PaperSessionResult,
    intents: tuple[BrokerIntent, ...],
    prints: Mapping[str, LivePrint],
) -> ShadowReport:
    fills_by_order: dict[str, list[Fill]] = {}
    for fill in session.fills:
        fills_by_order.setdefault(fill.order_id, []).append(fill)
    intent_order_ids = {intent.order_id for intent in intents}
    comparisons: list[IntentComparison] = []
    for intent in intents:
        would_be = tuple(fills_by_order.get(intent.order_id, ()))
        live = prints.get(intent.order_id)
        comparisons.append(_compare(intent, would_be, live))
    live_only = tuple(
        print_
        for order_id, print_ in prints.items()
        if order_id not in intent_order_ids
    )
    live_only = tuple(sorted(live_only, key=lambda print_: print_.order_id))
    return ShadowReport(
        run_id=run_id,
        deployment_mode=deployment_mode,
        session=session,
        intents=intents,
        comparisons=tuple(comparisons),
        live_only_prints=live_only,
    )


def _compare(
    intent: BrokerIntent,
    would_be: tuple[Fill, ...],
    live: LivePrint | None,
) -> IntentComparison:
    order = intent.order
    if not would_be and live is None:
        return IntentComparison(
            intent_sequence=intent.sequence,
            order_id=intent.order_id,
            verdict=ShadowVerdict.UNFILLED,
            would_be=(),
            live=None,
            detail="no simulated fill and no live print",
        )
    if not would_be:
        assert live is not None
        return IntentComparison(
            intent_sequence=intent.sequence,
            order_id=intent.order_id,
            verdict=ShadowVerdict.DIVERGED,
            would_be=(),
            live=live,
            detail="no simulated fill; live printed",
        )
    if live is None:
        return IntentComparison(
            intent_sequence=intent.sequence,
            order_id=intent.order_id,
            verdict=ShadowVerdict.WOULD_BE_ONLY,
            would_be=would_be,
            live=None,
            detail="no live print available",
        )
    detail = _first_would_be_difference(order, would_be, live)
    verdict = ShadowVerdict.DIVERGED if detail is not None else ShadowVerdict.MATCHED
    return IntentComparison(
        intent_sequence=intent.sequence,
        order_id=intent.order_id,
        verdict=verdict,
        would_be=would_be,
        live=live,
        detail=detail,
    )


def _first_would_be_difference(
    order: SingleLegOrder, would_be: tuple[Fill, ...], live: LivePrint
) -> str | None:
    """Return the first pinned-field disagreement between the two outcomes.

    Symbol, side, and quantity are always pinned.  Price is pinned only for
    single-fill intents: the pessimistic fill plugins fill routable orders
    in one exact print, so a single simulated fill must match the live
    print's price exactly; multi-fill intents are compared on quantity
    only and noted.
    """
    quantity = sum((fill.quantity for fill in would_be), start=Decimal("0"))
    if str(order.symbol) != live.symbol:
        return f"symbol {order.symbol} != {live.symbol}"
    if order.side is not live.side:
        return f"side {order.side.value} != {live.side.value}"
    if quantity != live.quantity:
        return f"quantity {quantity} != {live.quantity}"
    if len(would_be) == 1 and would_be[0].price != live.price:
        return f"price {would_be[0].price} != {live.price}"
    return None


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------


def run_shadow_session(
    configuration: TraderConfig,
    feed: PaperFeed,
    *,
    options: PaperOptions,
    live_prints: Sequence[LivePrint] = (),
    sink: ObserveOnlyBrokerSink | None = None,
    strategy_loader: StrategyLoader | None = None,
    fee_schedule: FeeSchedule | None = None,
    risk_pipeline: RiskPipeline | None = None,
    wall_clock: Clock | None = None,
) -> ShadowReport:
    """Compose one shadow session, drive it from ``feed``, and report."""
    shell = ShadowShell(
        configuration,
        options=options,
        sink=sink,
        strategy_loader=strategy_loader,
        fee_schedule=fee_schedule,
        risk_pipeline=risk_pipeline,
        wall_clock=wall_clock,
    )
    return shell.run(feed, live_prints=live_prints)


__all__ = [
    "BrokerIntent",
    "IntentComparison",
    "LivePrint",
    "ObserveOnlyBrokerSink",
    "ShadowConfigurationError",
    "ShadowError",
    "ShadowReport",
    "ShadowShell",
    "ShadowVerdict",
    "run_shadow_session",
]
