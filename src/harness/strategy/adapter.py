"""Platform adapter for workspace-published strategy source and Layer B.

The adapter keeps the strategy and research model as separate artifacts. It
loads the strategy source passed in the workspace-scoped run configuration,
delegates ordinary callbacks and orders to that strategy, and owns the frozen
research monitor. A falsifier hit stops new callbacks and submits market orders
that flatten positions held by the strategy book.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from harness.core.symbols import Symbol
from harness.core.types import (
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
    TradeEvent,
)
from harness.research.adapter import ResearchModelMonitor
from harness.strategy.contracts import Strategy, StrategyContext


class StrategyBundleAdapter(Strategy):
    """Delegate workspace strategy source while enforcing Layer B exits."""

    def __init__(
        self,
        *,
        strategy_source: Mapping[str, str],
        strategy_entrypoint: str,
        strategy_parameters: Mapping[str, object] | None = None,
        model_monitor: Mapping[str, object] | None = None,
        exit_criteria: Sequence[Mapping[str, object]] = (),
    ) -> None:
        if not isinstance(strategy_source, Mapping) or not strategy_source:
            raise ValueError("strategy_source must be a non-empty mapping")
        if (
            not isinstance(strategy_entrypoint, str)
            or not strategy_entrypoint.isidentifier()
        ):
            raise ValueError("strategy_entrypoint must be a class identifier")
        namespace: dict[str, object] = {"__name__": "workspace_strategy"}
        for path, source in sorted(strategy_source.items()):
            if not isinstance(path, str) or not isinstance(source, str):
                raise TypeError("strategy source must be a path-to-text mapping")
            exec(  # noqa: S102 - source has passed the strategy validation boundary
                compile(source, filename=path, mode="exec"), namespace
            )
        strategy_class = namespace.get(strategy_entrypoint)
        if not isinstance(strategy_class, type):
            raise ValueError(
                f"strategy entrypoint {strategy_entrypoint!r} is not a class"
            )
        parameters = dict(strategy_parameters or {})
        try:
            self._delegate = strategy_class(**parameters)
        except TypeError as first_error:
            try:
                self._delegate = strategy_class(parameters=parameters)
            except TypeError:
                raise first_error from None
        monitor_config = dict(model_monitor or {})
        self._monitor = (
            None
            if not monitor_config
            else ResearchModelMonitor(
                model_source=_text(monitor_config, "model_source"),
                model_fit_entrypoint=_text(
                    monitor_config, "model_fit_entrypoint", "fit"
                ),
                model_observe_entrypoint=_text(
                    monitor_config, "model_observe_entrypoint", "observe"
                ),
                fit=_numeric_mapping(monitor_config.get("fit")),
                indicators=_bindings(monitor_config.get("indicators")),
            )
        )
        self._exit_criteria = tuple(dict(item) for item in exit_criteria)
        self._context: StrategyContext | None = None
        self._sequence = 0
        self._falsified = False

    def on_start(self, context: StrategyContext) -> None:
        self._context = context
        self._delegate.on_start(context)

    def on_quote(self, event: QuoteEvent) -> None:
        self._sequence = event.sequence
        if self._monitor is not None:
            self._monitor.on_quote(event)
        if self._check_layer_b():
            return
        self._delegate.on_quote(event)

    def on_trade(self, event: TradeEvent) -> None:
        self._sequence = event.sequence
        if self._monitor is not None:
            self._monitor.on_trade(event)
        if self._check_layer_b():
            return
        self._delegate.on_trade(event)

    def on_fill(self, event: object) -> None:
        self._delegate.on_fill(event)

    def on_stop(self, context: StrategyContext) -> None:
        self._check_layer_b()
        self._delegate.on_stop(context)

    def _check_layer_b(self) -> bool:
        if self._falsified or self._monitor is None or self._context is None:
            return self._falsified
        observations = self._monitor.observe()
        for name, value in observations.items():
            self._context.publish_metric(name, value)
        for criterion in self._exit_criteria:
            name = criterion.get("observable")
            if not isinstance(name, str) or name not in observations:
                continue
            if _falsifies(
                observations[name],
                criterion.get("condition"),
                criterion.get("threshold"),
            ):
                self._falsified = True
                self._context.state["layer_b_falsified"] = name
                self._flatten()
                callback = getattr(self._delegate, "on_model_falsified", None)
                if callable(callback):
                    callback(name, observations[name], self._context)
                break
        return self._falsified

    def _flatten(self) -> None:
        assert self._context is not None
        for symbol in self._context.universe.symbols:
            position = self._context.books.position(symbol).quantity
            if position == Decimal("0"):
                continue
            side = Side.SELL if position > Decimal("0") else Side.BUY
            order_id = f"layer-b-exit-{symbol}-{self._sequence}"
            self._context.orders.submit(
                SingleLegOrder(
                    event_id=order_id,
                    timestamp=self._context.clock.now(),
                    sequence=self._sequence,
                    order_id=order_id,
                    symbol=Symbol(symbol),
                    side=side,
                    quantity=abs(position),
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                )
            )


def _text(values: Mapping[str, object], key: str, default: str | None = None) -> str:
    value = values.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"model monitor requires {key}")
    return value.strip()


def _numeric_mapping(value: object) -> Mapping[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("model monitor requires a non-empty frozen fit")
    result: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or isinstance(item, bool):
            raise TypeError("frozen fit must contain numeric named values")
        result[key] = float(item)
    return result


def _bindings(value: object) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError("model monitor indicators must be a sequence")
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _falsifies(value: float, condition: object, threshold: object) -> bool:
    if isinstance(threshold, bool):
        return False
    if isinstance(threshold, str):
        try:
            threshold = float(threshold)
        except ValueError:
            return False
    if not isinstance(threshold, int | float):
        return False
    if condition == "lt":
        return value < threshold
    if condition == "lte":
        return value <= threshold
    if condition == "gt":
        return value > threshold
    if condition == "gte":
        return value >= threshold
    return False


__all__ = ["StrategyBundleAdapter"]
