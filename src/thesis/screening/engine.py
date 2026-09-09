"""Point-in-time symbol screening engine.

The engine evaluates screener definitions over the as-of universe snapshot
only: market-data windows end at the screening date, and a bar stamped after
``as_of`` is invisible no matter what the injected port returns. Filters are
applied in declaration order and the surviving symbols are ranked with a
deterministic ordering (primary metric descending, symbol ascending on
ties), so identical inputs produce byte-identical results.

The engine consumes data exclusively through injected
:class:`ScreenerContext` ports; it performs no I/O, reads no wall clock, and
draws no randomness. Provenance (universe revision, data window, definition
hash) is carried on every result so a screen can be cited exactly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Protocol

SCHEMA_VERSION: Final = 1
DEFINITION_DOMAIN: Final = b"fca:thesis-screening-definition:v1\0"
RESULT_DOMAIN: Final = b"fca:thesis-screening-result:v1\0"


class ScreenerError(RuntimeError):
    """Base class for screening failures."""


class ScreenerPortError(ScreenerError):
    """A data port failed while the engine was screening."""


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    """One as-of universe snapshot listing."""

    symbol: str
    listing_date: date
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MarketBar:
    """One market bar visible to the screener."""

    bar_date: date
    volume: int
    close: str
    dollar_volume: str


class ScreenerContext(Protocol):
    """Injected data ports the engine screens over."""

    def universe(self, as_of: date) -> Sequence[UniverseEntry]:
        """Return the point-in-time universe snapshot for ``as_of``."""
        ...

    def market_data(self, symbol: str, window_end: date) -> Sequence[MarketBar]:
        """Return bars ending at ``window_end`` for one symbol."""
        ...


@dataclass(frozen=True, slots=True)
class ScreenerDefinition:
    """A validated screener definition.

    ``filters`` is an ordered tuple of ``{"kind": <preset name>, "params":
    {...}}`` mappings applied in declaration order. ``universe_revision``
    is the point-in-time universe snapshot identifier the definition
    targets; it flows verbatim into result provenance.
    """

    name: str
    universe_revision: str
    filters: tuple[Mapping[str, Any], ...]
    lookback_days: int = 20

    def __post_init__(self) -> None:
        from thesis.screening.presets import resolve_filter

        if not self.name:
            raise ScreenerError("definition name must not be empty")
        if not self.universe_revision:
            raise ScreenerError("universe revision must not be empty")
        if isinstance(self.lookback_days, bool) or self.lookback_days < 1:
            raise ScreenerError("lookback_days must be a positive integer")
        if not self.filters:
            raise ScreenerError("definition must declare at least one filter")
        for spec in self.filters:
            if not isinstance(spec, Mapping):
                raise ScreenerError("each filter must be a mapping")
            resolve_filter(spec.get("kind"), spec.get("params") or {})

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON-ready definition document."""
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "universe_revision": self.universe_revision,
            "lookback_days": self.lookback_days,
            "filters": [dict(spec) for spec in self.filters],
        }

    @property
    def definition_hash(self) -> str:
        """Return the content hash of the canonical definition document."""
        payload = json.dumps(
            self.canonical_document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(DEFINITION_DOMAIN + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ScreenerHit:
    """One surviving symbol with its evaluated metrics."""

    symbol: str
    rank: int
    metrics: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ScreenerResult:
    """The complete deterministic outcome of one screen."""

    definition_name: str
    universe_revision: str
    as_of: date
    data_window: tuple[date, date]
    definition_hash: str
    hits: tuple[ScreenerHit, ...]

    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON-ready result document."""
        return {
            "schema_version": SCHEMA_VERSION,
            "definition_name": self.definition_name,
            "universe_revision": self.universe_revision,
            "as_of": self.as_of.isoformat(),
            "data_window_start": self.data_window[0].isoformat(),
            "data_window_end": self.data_window[1].isoformat(),
            "definition_hash": self.definition_hash,
            "hits": [
                {
                    "rank": hit.rank,
                    "symbol": hit.symbol,
                    "metrics": dict(hit.metrics),
                }
                for hit in self.hits
            ],
        }

    def canonical_json(self) -> str:
        """Return the deterministic JSON serialization of the result."""
        return json.dumps(
            self.canonical_document(), sort_keys=True, separators=(",", ":")
        )


def _bars_through(bars: Sequence[MarketBar], as_of: date) -> tuple[MarketBar, ...]:
    """Return the bars visible at ``as_of`` (strictly no lookahead)."""
    visible = tuple(bar for bar in bars if bar.bar_date <= as_of)
    return tuple(sorted(visible, key=lambda bar: bar.bar_date))


def _symbol_metrics(
    entry: UniverseEntry,
    bars: Sequence[MarketBar],
    as_of: date,
) -> dict[str, str]:
    if not bars:
        raise ScreenerError(f"{entry.symbol}: no market data in the window")
    closes = [bar.close for bar in bars]
    dollar = [bar.dollar_volume for bar in bars]
    return {
        "avg_dollar_volume": str(
            sum((float(value) for value in dollar), 0.0) / len(dollar)
        ),
        "min_close": str(min(float(value) for value in closes)),
        "last_close": str(float(closes[-1])),
        "listing_age_days": str((as_of - entry.listing_date).days),
        "avg_volume": str(sum((bar.volume for bar in bars), 0) / len(bars)),
    }


def run_screener(
    definition: ScreenerDefinition,
    context: ScreenerContext,
    *,
    as_of: date,
) -> ScreenerResult:
    """Evaluate ``definition`` over the as-of universe snapshot."""
    from thesis.screening.presets import resolve_filter

    if not isinstance(definition, ScreenerDefinition):
        raise ScreenerError("definition must be a ScreenerDefinition")
    window_start = as_of.toordinal() - (definition.lookback_days - 1)
    window_start_date = date.fromordinal(window_start)
    try:
        entries = tuple(context.universe(as_of))
    except ScreenerError:
        raise
    except Exception as error:  # noqa: BLE001 - port failures fail closed
        raise ScreenerPortError(f"universe port failed: {error}") from error

    metrics_by_symbol: dict[str, dict[str, str]] = {}
    for entry in entries:
        try:
            bars = _bars_through(context.market_data(entry.symbol, as_of), as_of)
        except ScreenerPortError:
            raise
        except Exception as error:  # noqa: BLE001 - port failures fail closed
            raise ScreenerPortError(
                f"market-data port failed for {entry.symbol}: {error}"
            ) from error
        metrics_by_symbol[entry.symbol] = _symbol_metrics(entry, bars, as_of)

    # Average-volume percentile rank across the whole candidate set.
    volumes = sorted(
        float(metrics["avg_volume"]) for metrics in metrics_by_symbol.values()
    )

    def _percentile(value: str) -> float:
        rank = sum(1 for other in volumes if other <= float(value))
        return rank / len(volumes) if volumes else 0.0

    survivors: list[tuple[str, dict[str, str]]] = []
    for symbol, metrics in metrics_by_symbol.items():
        percentile = _percentile(str(metrics["avg_volume"]))
        passed = True
        for spec in definition.filters:
            evaluator = resolve_filter(spec.get("kind"), spec.get("params", {}))
            if not evaluator.evaluate(metrics, percentile=percentile):
                passed = False
                break
        if passed:
            survivors.append((symbol, metrics))

    survivors.sort(key=lambda item: (-float(item[1]["avg_dollar_volume"]), item[0]))
    hits = tuple(
        ScreenerHit(
            symbol=symbol,
            rank=index + 1,
            metrics={key: str(value) for key, value in metrics.items()},
        )
        for index, (symbol, metrics) in enumerate(survivors)
    )
    return ScreenerResult(
        definition_name=definition.name,
        universe_revision=definition.universe_revision,
        as_of=as_of,
        data_window=(window_start_date, as_of),
        definition_hash=definition.definition_hash,
        hits=hits,
    )


__all__ = [
    "DEFINITION_DOMAIN",
    "RESULT_DOMAIN",
    "SCHEMA_VERSION",
    "MarketBar",
    "ScreenerContext",
    "ScreenerDefinition",
    "ScreenerError",
    "ScreenerHit",
    "ScreenerPortError",
    "UniverseEntry",
    "run_screener",
]
