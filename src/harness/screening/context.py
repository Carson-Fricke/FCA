"""Host-side context builder for sandboxed screener evaluation.

The host assembles one :class:`FilterContext` per symbol: a security fact
sheet, the as-of OHLCV window, and named indicator series precomputed on
the host through ``harness.indicators.create_indicator``.  ``to_payload``
flattens the context into the plain JSON data the worker script observes;
the sandbox never sees harness objects, and indicator values arrive as
floats aligned with the bar window.

Serialization is deterministic: prices are exact ``Decimal`` values on the
host and convert to JSON floats at the boundary, volumes stay integers,
and dates serialize as ISO strings.  The same conversion runs for the
subprocess protocol and for the in-process reference executor, so both
execution models observe byte-identical data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from math import isfinite

from harness.core.symbols import canonical_symbol
from harness.core.types.events import TradeEvent
from harness.indicators import create_indicator

__all__ = [
    "BarRecord",
    "FilterContext",
    "IndicatorSeriesRequest",
    "SecurityFacts",
    "compute_indicator_series",
]


def _json_scalar(value: object, field_name: str) -> None:
    """Validate one JSON-scalar fundamental value."""
    if value is None or isinstance(value, str | bool):
        return
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{field_name} must be a finite float")
    if not isinstance(value, int | float):
        raise TypeError(
            f"{field_name} must be a JSON scalar (str, int, float, bool, or None)"
        )


@dataclass(frozen=True, slots=True)
class SecurityFacts:
    """The point-in-time fact sheet one screening context exposes.

    ``fundamentals`` carries the fundamental values as of ``as_of`` as
    JSON scalars; the sandbox observes them verbatim.
    """

    symbol: str
    security_type: str
    exchange: str
    sic_industry: str
    as_of: date
    fundamentals: Mapping[str, str | int | float | bool | None]

    def __post_init__(self) -> None:
        for name in ("symbol", "security_type", "exchange", "sic_industry"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.as_of, datetime) or not isinstance(self.as_of, date):
            raise ValueError("as_of must be a datetime.date")
        if not isinstance(self.fundamentals, Mapping) or isinstance(
            self.fundamentals, str
        ):
            raise TypeError("fundamentals must be a mapping of JSON scalars")
        for key, value in self.fundamentals.items():
            if not isinstance(key, str) or not key:
                raise ValueError("fundamental names must be non-empty strings")
            _json_scalar(value, f"fundamental {key!r}")


@dataclass(frozen=True, slots=True)
class BarRecord:
    """One OHLCV bar with exact decimal prices."""

    bar_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        if isinstance(self.bar_date, datetime) or not isinstance(self.bar_date, date):
            raise ValueError("bar_date must be a datetime.date")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int):
            raise TypeError("volume must be an integer")
        if self.volume <= 0:
            raise ValueError("volume must be positive")
        inner = min(self.open, self.close)
        outer = max(self.open, self.close)
        if self.low > inner or self.high < outer:
            raise ValueError("open and close must reconcile within low and high")


@dataclass(frozen=True, slots=True)
class FilterContext:
    """Everything one symbol's screening evaluation can observe.

    ``indicators`` maps a series name to one value per bar (``None`` where
    the indicator has not warmed up); the host precomputes it so scripts
    never hand-roll indicator math.
    """

    facts: SecurityFacts
    bars: tuple[BarRecord, ...] = ()
    indicators: Mapping[str, tuple[float | None, ...]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.facts, SecurityFacts):
            raise TypeError("facts must be a SecurityFacts")
        bars = tuple(self.bars)
        if any(not isinstance(bar, BarRecord) for bar in bars):
            raise TypeError("bars must contain BarRecord values")
        for earlier, later in zip(bars, bars[1:], strict=False):
            if earlier.bar_date >= later.bar_date:
                raise ValueError("bar dates must be strictly ascending")
        raw = self.indicators if self.indicators is not None else {}
        if not isinstance(raw, Mapping) or isinstance(raw, str):
            raise TypeError("indicators must be a mapping of series name to values")
        frozen: dict[str, tuple[float | None, ...]] = {}
        for name, values in raw.items():
            if not isinstance(name, str) or not name:
                raise ValueError("indicator series names must be non-empty strings")
            series = tuple(values)
            for value in series:
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, float):
                    raise TypeError(
                        f"indicator series {name!r} must hold floats or None"
                    )
                if not isfinite(value):
                    raise ValueError(f"indicator series {name!r} must be finite")
            if len(series) != len(bars):
                raise ValueError(
                    f"indicator series {name!r} must align with the bar window"
                )
            frozen[name] = series
        object.__setattr__(self, "bars", bars)
        object.__setattr__(self, "indicators", frozen)

    @property
    def symbol(self) -> str:
        """Return the screened symbol."""
        return self.facts.symbol

    def to_payload(self) -> dict[str, object]:
        """Flatten the context to the plain JSON data the script observes."""
        series = self.indicators if self.indicators is not None else {}
        return {
            "facts": {
                "symbol": self.facts.symbol,
                "security_type": self.facts.security_type,
                "exchange": self.facts.exchange,
                "sic_industry": self.facts.sic_industry,
                "as_of": self.facts.as_of.isoformat(),
                "fundamentals": dict(self.facts.fundamentals),
            },
            "bars": [
                {
                    "date": bar.bar_date.isoformat(),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": bar.volume,
                }
                for bar in self.bars
            ],
            "indicators": {name: list(values) for name, values in series.items()},
        }


@dataclass(frozen=True, slots=True)
class IndicatorSeriesRequest:
    """One declarative indicator request for the host-side series builder."""

    name: str
    params: Mapping[str, object] | None = None
    alias: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be a non-empty string")
        if self.alias is not None and (
            not isinstance(self.alias, str) or not self.alias
        ):
            raise ValueError("alias must be a non-empty string or None")
        if self.params is not None and (
            not isinstance(self.params, Mapping) or isinstance(self.params, str)
        ):
            raise TypeError("params must be a mapping")

    @property
    def series_key(self) -> str:
        """Return the deterministic series name the script observes."""
        if self.alias is not None:
            return self.alias
        params = self.params if self.params is not None else {}
        rendered = ", ".join(f"{key}={params[key]!r}" for key in sorted(params))
        return f"{self.name}({rendered})" if rendered else self.name


def compute_indicator_series(
    symbol: str,
    bars: Sequence[BarRecord],
    requests: Sequence[IndicatorSeriesRequest],
    *,
    bucket: str = "1d",
) -> dict[str, tuple[float | None, ...]]:
    """Precompute named indicator series over a daily bar window.

    Each bar contributes one trade at its close, fed through the registry
    indicator on the ``bucket`` emission grid; values land on the bar whose
    bucket start they belong to, with ``None`` before warm-up.  Values are
    exact indicator Decimals converted to floats at the sandbox boundary.
    """
    for request in requests:
        if not isinstance(request, IndicatorSeriesRequest):
            raise TypeError("requests must contain IndicatorSeriesRequest values")
    if not bars:
        return {request.series_key: () for request in requests}
    for earlier, later in zip(bars, bars[1:], strict=False):
        if not isinstance(earlier, BarRecord) or not isinstance(later, BarRecord):
            raise TypeError("bars must contain BarRecord values")
        if earlier.bar_date >= later.bar_date:
            raise ValueError("bar dates must be strictly ascending")
    index_by_date = {bar.bar_date: index for index, bar in enumerate(bars)}
    series: dict[str, tuple[float | None, ...]] = {}
    for request in requests:
        params = request.params if request.params is not None else {}
        indicator = create_indicator(
            request.name,
            {**dict(params), "bucket": bucket},
            symbol=symbol,
        )
        values: list[float | None] = [None] * len(bars)
        for index, bar in enumerate(bars):
            moment = datetime.combine(bar.bar_date, time(0, 0), tzinfo=UTC)
            indicator.on_event(
                TradeEvent(
                    f"sandbox:{symbol}:{index}",
                    moment,
                    index,
                    canonical_symbol(symbol),
                    bar.close,
                    Decimal(bar.volume),
                )
            )
        indicator.flush()
        for sample in indicator.samples():
            index = index_by_date.get(sample.start.date())
            if index is not None:
                values[index] = float(sample.value)
        series[request.series_key] = tuple(values)
    return series
