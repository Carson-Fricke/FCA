"""Strategy loading, identity, capability wiring, and guarded bindings.

The engine and test shells hand strategy contexts capability-narrowed
surfaces: a one-way :class:`StrategyVenue` command capability without
venue-state observation, and a :class:`StrategyScheduler` that refuses
market-data and outcome classes so strategy scheduling cannot fabricate
feed or execution events.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Final, cast

from harness.configuration import StrategyInstanceConfig, canonical_hash
from harness.core.events import EventBus
from harness.core.types import (
    AccountSnapshot,
    Book,
    Event,
    ExecutionReport,
    Fill,
    OrderRequest,
    Position,
    QuoteEvent,
    SingleLegOrder,
    TradeEvent,
)
from harness.data import LookaheadError
from harness.risk import RiskAction, RiskContext
from harness.runtime.backtest.errors import (
    BacktestAbort,
    StrategyHalt,
    StrategyLoadError,
)
from harness.runtime.backtest.manifest import StrategyIdentity
from harness.strategy import (
    Strategy,
    StrategyBinding,
    StrategyContext,
    bind_strategy,
)
from harness.venues import ExecutionVenue

if TYPE_CHECKING:
    from harness.runtime.backtest.engine import _BacktestEngine

type StrategyLoader = Callable[[StrategyInstanceConfig], type[Strategy]]


# --------------------------------------------------------------------------
# Strategy loading
# --------------------------------------------------------------------------


def load_strategy_class(implementation: str) -> type[Strategy]:
    """Load one ``module:Class`` strategy implementation deterministically."""
    if not isinstance(implementation, str) or ":" not in implementation:
        raise StrategyLoadError("strategy implementation must use module:Class syntax")
    module_name, class_name = implementation.rsplit(":", 1)
    if not module_name or not class_name:
        raise StrategyLoadError("strategy implementation must use module:Class syntax")
    try:
        module = import_module(module_name)
    except ImportError as error:
        raise StrategyLoadError(
            f"cannot import strategy module {module_name!r}"
        ) from error
    candidate = getattr(module, class_name, None)
    if not isinstance(candidate, type) or not issubclass(candidate, Strategy):
        raise StrategyLoadError(f"{implementation!r} is not a Strategy subclass")
    return cast("type[Strategy]", candidate)


def default_strategy_loader(instance: StrategyInstanceConfig) -> type[Strategy]:
    """Load the implementation configured for one strategy instance."""
    if not isinstance(instance, StrategyInstanceConfig):
        raise StrategyLoadError("instance must be a StrategyInstanceConfig")
    return load_strategy_class(instance.implementation)


def _strategy_identity(instance: StrategyInstanceConfig) -> StrategyIdentity:
    return StrategyIdentity(
        instance_id=instance.instance_id,
        implementation=instance.implementation,
        bundle_hash=instance.bundle_hash,
        parameter_hash=canonical_hash(dict(instance.parameters)),
        symbols=tuple(str(symbol) for symbol in instance.symbols),
        capital_fraction=str(instance.capital_fraction),
    )


# --------------------------------------------------------------------------
# Per-strategy exception sink and guarded binding
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _StrategySink:
    """Per-strategy exception and teardown record."""

    instance_id: str
    halt: StrategyHalt | None = None
    abort: BacktestAbort | None = None
    violation: LookaheadError | None = None
    error: Exception | None = None
    teardown_error: Exception | None = None
    stopped: bool = False

    def clear_requests(self) -> None:
        """Drop handled halt and abort requests."""
        self.halt = None
        self.abort = None


class _GuardedBinding(StrategyBinding):
    """One strategy binding whose callback exceptions are captured per strategy.

    The binding delegates to :func:`~harness.strategy.bind_strategy` for
    instantiation and context validation, then intercepts exceptions so the
    runner can apply its halt, abort, and teardown policies without losing the
    identity of the strategy that raised.
    """

    __slots__ = ("_sink",)

    def __init__(
        self, strategy: Strategy, context: StrategyContext, sink: _StrategySink
    ) -> None:
        super().__init__(strategy, context)
        self._sink = sink

    @classmethod
    def bind(
        cls,
        strategy_class: type[Strategy],
        parameters: Mapping[str, object] | None,
        context: StrategyContext,
        sink: _StrategySink,
    ) -> _GuardedBinding:
        """Bind one strategy class through the public harness binding helper."""
        binding = bind_strategy(strategy_class, parameters, context)
        return cls(binding.strategy, context, sink)

    @property
    def sink(self) -> _StrategySink:
        """Return this binding's exception sink."""
        return self._sink

    def start(self) -> None:
        """Attach and start the strategy, capturing callback exceptions."""
        try:
            super().start()
        except _HandledError as error:
            _record(self._sink, error)
        except Exception as error:  # noqa: BLE001 - reported through the taxonomy
            self._sink.error = error

    def stop(self) -> None:
        """Deliver ``on_stop`` once, capturing callback exceptions."""
        if not self.started:
            return
        try:
            super().stop()
        except Exception as error:  # noqa: BLE001 - reported through the taxonomy
            self._sink.teardown_error = error
        self._sink.stopped = True

    def _receive(self, event: Event) -> None:
        """Dispatch one event, capturing callback exceptions for the runner."""
        try:
            super()._receive(event)
        except _HandledError as error:
            _record(self._sink, error)
        except Exception as error:  # noqa: BLE001 - reported through the taxonomy
            self._sink.error = error


_HandledError = (StrategyHalt, BacktestAbort, LookaheadError)


def _record(sink: _StrategySink, error: Exception) -> None:
    if isinstance(error, StrategyHalt):
        sink.halt = error
    elif isinstance(error, BacktestAbort):
        sink.abort = error
    else:
        sink.violation = cast("LookaheadError", error)


# --------------------------------------------------------------------------
# Strategy capability boundary
# --------------------------------------------------------------------------

_RESERVED_EVENT_CLASSES: Final[tuple[type[Event], ...]] = (
    QuoteEvent,
    TradeEvent,
    ExecutionReport,
    Fill,
    Position,
    Book,
    AccountSnapshot,
)


class StrategyVenue:
    """One-way venue command capability without venue-state observation.

    Strategy contexts receive this capability instead of a concrete venue.
    Commands are fire-and-forget; every outcome arrives as a dispatched
    event. The capability exposes no venue state, so strategy code cannot
    observe pending decisions, call records, remaining script steps, or any
    other venue internal. Shells keep ownership of the concrete venue.
    """

    __slots__ = ("_venue",)

    def __init__(self, venue: ExecutionVenue) -> None:
        if not isinstance(venue, ExecutionVenue):
            raise TypeError("venue must implement ExecutionVenue")
        self._venue = venue

    def submit(self, order: OrderRequest) -> None:
        """Forward one submission; outcomes arrive as events."""
        self._venue.submit(order)

    def cancel(self, order_id: str) -> None:
        """Forward one cancellation; outcomes arrive as events."""
        self._venue.cancel(order_id)

    def replace(self, order_id: str, replacement: OrderRequest) -> None:
        """Forward one replacement; outcomes arrive as events."""
        self._venue.replace(order_id, replacement)

    def request_position_snapshot(self, account_id: str) -> None:
        """Forward one snapshot request; outcomes arrive as events."""
        self._venue.request_position_snapshot(account_id)


class _RiskVenue(StrategyVenue):
    """Run one strategy's orders through the risk pipeline before the venue."""

    __slots__ = (
        "_engine",
        "_instance_id",
    )

    def __init__(self, engine: _BacktestEngine, instance_id: str, venue: Any) -> None:
        super().__init__(venue)
        self._engine = engine
        self._instance_id = instance_id

    def submit(self, order: OrderRequest) -> None:
        """Evaluate and forward one order; outcomes arrive as events."""
        approved = self._admit(order)
        if approved is not None:
            self._venue.submit(approved)

    def replace(self, order_id: str, replacement: OrderRequest) -> None:
        """Evaluate and forward a replacement; outcomes arrive as events."""
        approved = self._admit(replacement)
        if approved is not None:
            self._venue.replace(order_id, approved)

    def _admit(self, order: OrderRequest) -> OrderRequest | None:
        engine = self._engine
        decision = engine.pipeline.evaluate(
            order,
            RiskContext(
                portfolio=engine.hub.snapshot,
                prices=engine.prices,
                daily_loss=engine.daily_loss(self._instance_id),
                order_count=engine.order_count(self._instance_id),
                strategy_id=self._instance_id,
            ),
        )
        engine.record_risk(decision)
        if decision.action is RiskAction.REJECT:
            engine.publish_rejection(order, decision)
            if decision.halt_scope == "halt_strategy":
                raise StrategyHalt(
                    f"risk halted strategy {self._instance_id!r}: {decision.reason}"
                )
            if decision.halt_scope == "halt_runtime":
                raise BacktestAbort(
                    f"risk halted the run for {self._instance_id!r}: {decision.reason}"
                )
            return None
        approved = decision.order
        if not isinstance(approved, SingleLegOrder):
            engine.publish_rejection(
                approved,
                decision,
                reason="multi-leg orders are not supported by the simulated venue",
            )
            return None
        engine.own_order(approved.order_id, self._instance_id)
        engine.count_order(self._instance_id)
        return approved


class StrategyScheduler:
    """Scheduler capability that refuses fabricated feed and outcome events.

    Market data and venue or portfolio outcomes are reserved for the feed and
    the venue. The capability rejects those event classes before they reach
    the event bus, so strategy scheduling cannot impersonate market data or
    fabricate execution outcomes. Provenance stamping of strategy-owned
    events remains the shell scheduler's responsibility.
    """

    __slots__ = ("_bus",)

    def __init__(self, bus: EventBus) -> None:
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        self._bus = bus

    def schedule(self, event: Event) -> None:
        """Schedule one strategy-owned event; reserved classes are refused."""
        if isinstance(event, _RESERVED_EVENT_CLASSES):
            raise TypeError(
                f"{type(event).__name__} events are reserved for the feed and "
                "the venue; strategies cannot schedule them"
            )
        self._bus.schedule(event)
