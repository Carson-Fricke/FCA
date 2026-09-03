"""Strategy loading, identity, and guarded per-strategy execution bindings."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from harness.configuration import StrategyInstanceConfig, canonical_hash
from harness.core.types import Event, OrderRequest, SingleLegOrder
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


class _RiskVenue:
    """Run one strategy's orders through the risk pipeline before the venue."""

    __slots__ = ("_engine", "_instance_id", "_venue")

    def __init__(self, engine: _BacktestEngine, instance_id: str, venue: Any) -> None:
        self._engine = engine
        self._instance_id = instance_id
        self._venue = venue

    def submit(self, order: OrderRequest) -> None:
        """Evaluate and forward one order; outcomes arrive as events."""
        approved = self._admit(order)
        if approved is not None:
            self._venue.submit(approved)

    def cancel(self, order_id: str) -> None:
        """Request cancellation through the simulated venue."""
        self._venue.cancel(order_id)

    def replace(self, order_id: str, replacement: OrderRequest) -> None:
        """Evaluate and forward a replacement; outcomes arrive as events."""
        approved = self._admit(replacement)
        if approved is not None:
            self._venue.replace(order_id, approved)

    def request_position_snapshot(self, account_id: str) -> None:
        """Validate the protocol command without fabricating an outcome."""
        self._venue.request_position_snapshot(account_id)

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
