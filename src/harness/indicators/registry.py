"""Typed indicator registry mapping declarative names to factories.

Screener filters, strategies, and other declarative consumers request
indicators by name and parameter mapping — ``create_indicator("sma",
{"period": 20}, symbol="AAPL")`` — and receive a fresh, symbol-bound
:class:`~harness.indicators.base.TickIndicator`.  Every parameter is
validated against the spec's typed schema before construction, so invalid
requests fail loudly instead of silently changing indicator semantics.

The shared ``bucket`` emission-interval parameter is appended to every spec
automatically; the default registry ships the built-in indicators and
callers can build private registries for custom extensions.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from enum import StrEnum
from typing import Final, Self

from harness.core.symbols import Symbol, canonical_symbol
from harness.data.bars import interval_delta
from harness.indicators.base import (
    MIDNIGHT_UTC,
    IndicatorConfigurationError,
    TickIndicator,
)
from harness.indicators.momentum import RelativeStrengthIndex
from harness.indicators.overlays import (
    ExponentialMovingAverage,
    MidSimpleMovingAverage,
    SimpleMovingAverage,
)
from harness.indicators.volatility import AverageTrueRange, RealizedVolatility
from harness.indicators.volume import (
    DollarVolume,
    OnBalanceVolume,
    VolumeWeightedAveragePrice,
)

_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*$")


class IndicatorParamKind(StrEnum):
    """Typed parameter vocabulary supported by indicator specs."""

    INTEGER = "integer"
    DURATION = "duration"


@dataclass(frozen=True, slots=True)
class IndicatorParam:
    """One typed indicator parameter declaration."""

    name: str
    kind: IndicatorParamKind
    required: bool = True
    default: int | str | timedelta | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("parameter name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip())
        if not isinstance(self.kind, IndicatorParamKind):
            raise TypeError("kind must be an IndicatorParamKind")
        if not isinstance(self.required, bool):
            raise TypeError("required must be a boolean")
        if self.default is not None and (
            isinstance(self.default, bool)
            or not isinstance(self.default, int | str | timedelta)
        ):
            raise TypeError("default must be None, an int, a str, or a timedelta")


IndicatorFactory = Callable[..., TickIndicator]


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """One registered indicator: name, typed parameter schema, and factory."""

    name: str
    description: str
    params: tuple[IndicatorParam, ...]
    factory: IndicatorFactory

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError("indicator name must match ^[a-z][a-z0-9_]*$")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("description must be a non-empty string")
        params = tuple(self.params)
        if not all(isinstance(param, IndicatorParam) for param in params):
            raise TypeError("params must contain IndicatorParam values")
        names = [param.name for param in params]
        if len(set(names)) != len(names):
            raise ValueError("parameter names must be unique")
        if any(param.name == "bucket" and param != _BUCKET_PARAM for param in params):
            raise ValueError("bucket is a reserved parameter managed by the registry")
        if not callable(self.factory):
            raise TypeError("factory must be callable")
        object.__setattr__(self, "params", params)


def _resolve_parameters(
    spec: IndicatorSpec, requested: Mapping[str, object]
) -> dict[str, object]:
    resolved: dict[str, object] = {}
    for param in spec.params:
        if param.name in requested:
            resolved[param.name] = _validate_value(param, requested[param.name])
        elif param.required:
            raise IndicatorConfigurationError(
                f"indicator {spec.name!r} requires parameter {param.name!r}"
            )
        elif param.default is not None:
            resolved[param.name] = _validate_value(param, param.default)
        else:
            resolved[param.name] = None
    unknown = sorted(set(requested) - {param.name for param in spec.params})
    if unknown:
        raise IndicatorConfigurationError(
            f"indicator {spec.name!r} does not accept parameters: {unknown}"
        )
    return resolved


def _validate_value(param: IndicatorParam, value: object) -> object:
    if value is None:
        return None
    if param.kind is IndicatorParamKind.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise IndicatorConfigurationError(
                f"parameter {param.name!r} must be an integer"
            )
        if value < 1:
            raise IndicatorConfigurationError(
                f"parameter {param.name!r} must be positive"
            )
        return value
    if not isinstance(value, str | timedelta):
        raise IndicatorConfigurationError(
            f"parameter {param.name!r} must be a duration string or timedelta"
        )
    try:
        return interval_delta(value)
    except (TypeError, ValueError) as error:
        raise IndicatorConfigurationError(
            f"parameter {param.name!r} must be a positive duration string or timedelta"
        ) from error


class IndicatorRegistry:
    """A name-to-spec registry with typed parameter validation."""

    __slots__ = ("_frozen", "_specs")

    def __init__(self, *, frozen: bool = False) -> None:
        self._specs: dict[str, IndicatorSpec] = {}
        self._frozen = frozen

    @property
    def frozen(self) -> bool:
        """Return whether registrations are permanently closed."""
        return self._frozen

    def freeze(self) -> Self:
        """Permanently close registration and return this registry."""
        self._frozen = True
        return self

    def register(self, spec: IndicatorSpec) -> None:
        """Register one spec, rejecting duplicate names."""
        if self._frozen:
            raise IndicatorConfigurationError("indicator registry is immutable")
        if not isinstance(spec, IndicatorSpec):
            raise TypeError("spec must be an IndicatorSpec")
        if spec.name in self._specs:
            raise IndicatorConfigurationError(
                f"indicator {spec.name!r} is already registered"
            )
        self._specs[spec.name] = spec

    def spec(self, name: str) -> IndicatorSpec:
        """Return the spec registered under ``name``."""
        if not isinstance(name, str) or not name.strip():
            raise IndicatorConfigurationError("indicator name must be a string")
        spec = self._specs.get(name.strip())
        if spec is None:
            raise IndicatorConfigurationError(f"unknown indicator {name.strip()!r}")
        return spec

    def names(self) -> tuple[str, ...]:
        """Return registered indicator names in lexical order."""
        return tuple(sorted(self._specs))

    def create(
        self,
        name: str,
        params: Mapping[str, object] | None = None,
        *,
        symbol: str | Symbol,
        session_anchor: time | None = MIDNIGHT_UTC,
    ) -> TickIndicator:
        """Build a fresh symbol-bound indicator from a validated request."""
        spec = self.spec(name)
        resolved = _resolve_parameters(spec, dict(params or {}))
        return spec.factory(
            symbol=canonical_symbol(symbol),
            session_anchor=session_anchor,
            **resolved,
        )


_BUCKET_PARAM: Final = IndicatorParam(
    "bucket", IndicatorParamKind.DURATION, required=False, default="1min"
)


def _builtin_spec(
    name: str,
    description: str,
    factory: IndicatorFactory,
    *params: IndicatorParam,
) -> IndicatorSpec:
    return IndicatorSpec(name, description, tuple(params) + (_BUCKET_PARAM,), factory)


def _build_default_registry() -> IndicatorRegistry:
    registry = IndicatorRegistry()
    registry.register(
        _builtin_spec(
            "sma",
            "Rolling simple mean of the last period trade prices.",
            SimpleMovingAverage,
            IndicatorParam("period", IndicatorParamKind.INTEGER),
        )
    )
    registry.register(
        _builtin_spec(
            "ema",
            "Recursive exponential mean of trade prices seeded on the first trade.",
            ExponentialMovingAverage,
            IndicatorParam("span", IndicatorParamKind.INTEGER),
        )
    )
    registry.register(
        _builtin_spec(
            "rsi",
            "Wilder relative strength index over consecutive trade-price changes.",
            RelativeStrengthIndex,
            IndicatorParam("period", IndicatorParamKind.INTEGER),
        )
    )
    registry.register(
        _builtin_spec(
            "atr",
            "Wilder average true range over per-bucket trade ranges.",
            AverageTrueRange,
            IndicatorParam("period", IndicatorParamKind.INTEGER),
        )
    )
    registry.register(
        _builtin_spec(
            "realized_vol",
            "Rolling standard deviation of tick-to-tick simple returns.",
            RealizedVolatility,
            IndicatorParam("window", IndicatorParamKind.DURATION),
        )
    )
    registry.register(
        _builtin_spec(
            "dollar_volume",
            "Traded notional; cumulative per session, or rolling over a window.",
            DollarVolume,
            IndicatorParam("window", IndicatorParamKind.DURATION, required=False),
        )
    )
    registry.register(
        _builtin_spec(
            "vwap",
            "Session-cumulative volume-weighted average price of trades.",
            VolumeWeightedAveragePrice,
        )
    )
    registry.register(
        _builtin_spec(
            "obv",
            "On-balance volume from the tick rule, cumulative per session.",
            OnBalanceVolume,
        )
    )
    registry.register(
        _builtin_spec(
            "mid_sma",
            "Rolling simple mean of the last period quote mid-prices.",
            MidSimpleMovingAverage,
            IndicatorParam("period", IndicatorParamKind.INTEGER),
        )
    )
    return registry.freeze()


DEFAULT_REGISTRY: Final = _build_default_registry()


def create_indicator(
    name: str,
    params: Mapping[str, object] | None = None,
    *,
    symbol: str | Symbol,
    session_anchor: time | None = MIDNIGHT_UTC,
) -> TickIndicator:
    """Build an indicator from the default registry by declarative name."""
    return DEFAULT_REGISTRY.create(
        name, params, symbol=symbol, session_anchor=session_anchor
    )


def indicator_names() -> tuple[str, ...]:
    """Return every built-in indicator name in lexical order."""
    return DEFAULT_REGISTRY.names()


def indicator_spec(name: str) -> IndicatorSpec:
    """Return the built-in spec registered under ``name``."""
    return DEFAULT_REGISTRY.spec(name)


__all__ = [
    "DEFAULT_REGISTRY",
    "IndicatorFactory",
    "IndicatorParam",
    "IndicatorParamKind",
    "IndicatorRegistry",
    "IndicatorSpec",
    "create_indicator",
    "indicator_names",
    "indicator_spec",
]
