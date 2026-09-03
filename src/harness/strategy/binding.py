"""Bind strategy classes to a synchronous event bus without a runtime runner."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import cast

from harness.core.types import (
    AccountSnapshot,
    Book,
    Event,
    ExecutionReport,
    Fill,
    MultiLegOrder,
    Position,
    QuoteEvent,
    SingleLegOrder,
    TradeEvent,
)
from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy


def instantiate_strategy[T: Strategy](
    strategy_class: type[T], parameters: Mapping[str, object] | None = None
) -> T:
    """Instantiate a strategy class from validated configuration parameters."""
    if not isinstance(strategy_class, type) or not issubclass(strategy_class, Strategy):
        raise TypeError("strategy_class must derive from Strategy")
    if parameters is None:
        values: Mapping[str, object] = {}
    elif not isinstance(parameters, Mapping):
        raise TypeError("parameters must be a mapping")
    else:
        values = MappingProxyType(dict(parameters))

    constructor = cast(Callable[..., T], strategy_class)
    signature = inspect.signature(strategy_class)
    try:
        signature.bind(**values)
    except TypeError as error:
        parameter = signature.parameters.get("parameters")
        if parameter is None or parameter.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            raise error
        signature.bind(parameters=values)
        return constructor(parameters=values)
    return constructor(**dict(values))


class StrategyBinding:
    """One strategy instance connected to all typed event callbacks."""

    __slots__ = ("_accounted_fill_ids", "_attached", "_context", "_started", "strategy")

    def __init__(self, strategy: Strategy, context: StrategyContext) -> None:
        if not isinstance(strategy, Strategy):
            raise TypeError("strategy must derive from Strategy")
        if not isinstance(context, StrategyContext):
            raise TypeError("context must be a StrategyContext")
        if context._bus_for_binding() is None:  # noqa: SLF001
            raise ValueError("strategy binding requires a context event bus")
        self.strategy = strategy
        self._context = context
        self._attached = False
        self._started = False
        self._accounted_fill_ids: set[str] = set()

    @property
    def context(self) -> StrategyContext:
        """Return the constrained context bound to this strategy."""
        return self._context

    @property
    def started(self) -> bool:
        """Return whether the strategy has received ``on_start``."""
        return self._started

    def attach(self) -> None:
        """Register the binding once; this does not start callbacks."""
        if self._attached:
            return
        bus = self._context._bus_for_binding()  # noqa: SLF001
        assert bus is not None
        bus.subscribe(Event, self._receive)
        self._attached = True

    def start(self) -> None:
        """Attach and invoke the strategy's start callback."""
        if self._started:
            raise RuntimeError("strategy binding is already started")
        self.attach()
        self._started = True
        self.strategy.on_start(self._context)

    def stop(self) -> None:
        """Invoke the strategy's stop callback once."""
        if not self._started:
            return
        self._started = False
        self.strategy.on_stop(self._context)

    def _receive(self, event: Event) -> None:
        if not self._started:
            return
        if isinstance(event, Fill):
            self._context._account_fill(event, self._accounted_fill_ids)  # noqa: SLF001
        if isinstance(event, AccountSnapshot):
            self._context._account_event(event)  # noqa: SLF001
        if not self._context._accepts(event):  # noqa: SLF001
            return
        callback_name = _callback_name(event)
        callback = getattr(self.strategy, callback_name, None)
        if callback is not None:
            callback(event)

    @classmethod
    def from_class[T: Strategy](
        cls,
        strategy_class: type[T],
        parameters: Mapping[str, object] | None,
        context: StrategyContext,
    ) -> StrategyBinding:
        """Instantiate and bind a strategy class using configuration values."""
        return cls(instantiate_strategy(strategy_class, parameters), context)


def bind_strategy[T: Strategy](
    strategy_class: type[T],
    parameters: Mapping[str, object] | None,
    context: StrategyContext,
) -> StrategyBinding:
    """Create a not-yet-started binding from a class and parameters."""
    return StrategyBinding.from_class(strategy_class, parameters, context)


def _callback_name(event: Event) -> str:
    if isinstance(event, QuoteEvent):
        return "on_quote"
    if isinstance(event, TradeEvent):
        return "on_trade"
    if isinstance(event, SingleLegOrder | MultiLegOrder):
        return "on_order"
    if isinstance(event, ExecutionReport):
        return "on_execution_report"
    if isinstance(event, Fill):
        return "on_fill"
    if isinstance(event, Position):
        return "on_position"
    if isinstance(event, Book):
        return "on_book"
    if isinstance(event, AccountSnapshot):
        return "on_account"
    return "on_event"


__all__ = [
    "StrategyBinding",
    "bind_strategy",
    "instantiate_strategy",
]
