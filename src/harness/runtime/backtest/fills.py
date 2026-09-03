"""Pessimistic fill plugin selection for the backtest runner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from harness.fills import (
    FillDecision,
    LatencySize,
    RemainderPolicy,
    SimpleTouch,
    TradeThrough,
)
from harness.runtime.backtest.errors import (
    _MICROSECOND,
    _ZERO,
    FillModel,
)

# --------------------------------------------------------------------------
# Fill plugin selection
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FillModelOptions:
    """Exact, deterministic options shared by the pessimistic fill plugins."""

    response_latency: timedelta = timedelta(0)
    remainder_policy: RemainderPolicy = RemainderPolicy.REST
    require_trade_through: bool = True
    minimum_trade_through: Decimal = _ZERO
    strict: bool = True


type FillPlugin = Callable[..., FillDecision | None]


def build_fill_model(
    model: FillModel | str = FillModel.SIMPLE_TOUCH,
    options: FillModelOptions | None = None,
) -> FillPlugin:
    """Return one deterministic fill plugin selected by name.

    ``simple_touch`` fills at the first visible touch.  ``latency_size``
    additionally requires a qualifying print for a resting limit, which is the
    pessimistic default for realistic simulations.  ``trade_through`` is the
    strictest variant and never fills from a quote alone.
    """
    selected = FillModel(model)
    chosen = FillModelOptions() if options is None else options
    if not isinstance(chosen, FillModelOptions):
        raise TypeError("options must be FillModelOptions")
    if selected is FillModel.SIMPLE_TOUCH:
        return SimpleTouch()
    if selected is FillModel.LATENCY_SIZE:
        return LatencySize(
            response_latency=chosen.response_latency,
            remainder_policy=chosen.remainder_policy,
            require_trade_through=chosen.require_trade_through,
        )
    return TradeThrough(
        response_latency=chosen.response_latency,
        minimum_trade_through=chosen.minimum_trade_through,
        strict=chosen.strict,
        remainder_policy=chosen.remainder_policy,
    )


def fill_model_identity(plugin: object) -> dict[str, object]:
    """Return a canonical, JSON-ready description of one fill plugin."""
    if isinstance(plugin, SimpleTouch):
        return {"model": FillModel.SIMPLE_TOUCH.value}
    if isinstance(plugin, LatencySize):
        return {
            "model": FillModel.LATENCY_SIZE.value,
            "response_latency_us": _latency_microseconds(plugin.response_latency),
            "remainder_policy": plugin.remainder_policy.value,
            "require_trade_through": plugin.require_trade_through,
        }
    if isinstance(plugin, TradeThrough):
        return {
            "model": FillModel.TRADE_THROUGH.value,
            "response_latency_us": _latency_microseconds(plugin.response_latency),
            "minimum_trade_through": str(plugin.minimum_trade_through),
            "strict": plugin.strict,
            "remainder_policy": plugin.remainder_policy.value,
        }
    return {"model": type(plugin).__name__}


def fill_model_latency(plugin: object) -> timedelta:
    """Return the deterministic response delay declared by a fill plugin."""
    latency = getattr(plugin, "latency", None)
    return latency if isinstance(latency, timedelta) else timedelta(0)


def _latency_microseconds(value: timedelta) -> int:
    return value // _MICROSECOND
