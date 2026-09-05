"""Deterministic sweep ranking and parameter sensitivity statistics.

Both statistics consume cataloged
:class:`~harness.results.store.RunCatalogRecord` rows joined against a sweep's
:class:`~harness.sweeps.spec.SweepPoint` values, use exact
:class:`decimal.Decimal` arithmetic throughout, and order every output with
deterministic tie-breaks, so identical inputs always produce identical
reports.

Exact statistics, by definition:

- **Ranking** orders the runs carrying the requested headline metric by that
  metric's exact decimal value (descending for ``descending=True``), with
  ties broken by run ID ascending. Ranks are one-based ordinal positions:
  equal values receive equal value text but distinct sequential ranks in
  run-ID order. Rows without a parseable finite metric value are excluded.
- **Parameter sensitivity** is a one-way main-effect summary per parameter:
  cataloged runs are grouped by each parameter's canonical level text, each
  level's statistic is the arithmetic mean of the metric over its runs, the
  parameter's effect is the range ``max(level mean) - min(level mean)``, and
  the best level is the maximum mean with ties broken by level text
  ascending. Level texts reuse the canonical JSON scalar encodings from
  :mod:`harness.configuration.hashing`, so a decimal ``0.05`` renders as
  ``{"$decimal":"0.05"}`` and a string ``"0.05"`` as ``"0.05"`` — distinct,
  string-exact levels with no float coercion. Level means and the grand mean
  use ``Decimal`` division under the default context, which is deterministic
  for identical inputs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from harness.configuration.hashing import canonical_configuration_json
from harness.core.types.common import text
from harness.results.store import RunCatalogRecord
from harness.sweeps.spec import SweepPoint


def _metric_value(record: RunCatalogRecord, metric: str) -> Decimal | None:
    raw = record.metrics.get(metric)
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _usable_points(
    points: Iterable[SweepPoint],
    records: Iterable[RunCatalogRecord],
    metric: str,
) -> tuple[tuple[SweepPoint, Decimal], ...]:
    parameters = {point.run_id: point for point in points}
    usable: list[tuple[SweepPoint, Decimal]] = []
    for record in records:
        point = parameters.get(record.run_id)
        if point is None:
            continue
        value = _metric_value(record, metric)
        if value is not None:
            usable.append((point, value))
    return tuple(usable)


@dataclass(frozen=True, slots=True)
class RankedRun:
    """One ranked catalog row for the requested headline metric."""

    rank: int
    run_id: str
    value: Decimal
    configuration_digest: str | None


def rank_runs(
    records: Iterable[RunCatalogRecord],
    metric: str,
    *,
    descending: bool = True,
) -> tuple[RankedRun, ...]:
    """Rank catalog rows by one metric with deterministic tie-breaks.

    Rows are ordered by metric value (descending unless ``descending`` is
    ``False``), then by run ID ascending. One-based ordinal ranks follow the
    sorted order; rows missing the metric or carrying a non-decimal or
    non-finite value are excluded.
    """
    metric = text(metric, "metric")
    scored = [
        (record, value)
        for record in records
        if (value := _metric_value(record, metric)) is not None
    ]
    ordered = sorted(
        scored,
        key=lambda item: (-item[1] if descending else item[1], item[0].run_id),
    )
    return tuple(
        RankedRun(
            rank=index,
            run_id=record.run_id,
            value=value,
            configuration_digest=record.configuration_digest,
        )
        for index, (record, value) in enumerate(ordered, start=1)
    )


def _level_text(value: object) -> str:
    """Return the canonical level label for one parameter value."""
    return canonical_configuration_json(value)


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    total = values[0]
    for value in values[1:]:
        total += value
    return total / len(values)


@dataclass(frozen=True, slots=True)
class ParameterLevelMean:
    """Per-level mean of the metric over the runs sharing that level."""

    value: str
    mean: Decimal
    count: int


@dataclass(frozen=True, slots=True)
class ParameterSensitivity:
    """One-way main-effect summary for one swept parameter."""

    name: str
    levels: tuple[ParameterLevelMean, ...]
    grand_mean: Decimal
    effect_range: Decimal
    best_value: str


@dataclass(frozen=True, slots=True)
class SweepSensitivity:
    """Complete sensitivity summary of one sweep for one metric."""

    metric: str
    runs_used: int
    grand_mean: Decimal
    parameters: tuple[ParameterSensitivity, ...]


def parameter_sensitivity(
    points: Iterable[SweepPoint],
    records: Iterable[RunCatalogRecord],
    metric: str,
) -> SweepSensitivity:
    """Summarize per-parameter effects from the sweep's cataloged results.

    Only runs present in ``points`` and carrying a parseable finite
    ``metric`` value contribute. Parameters are reported in sorted name
    order with levels sorted by canonical level text; see the module
    docstring for the exact statistics.
    """
    metric = text(metric, "metric")
    usable = _usable_points(points, records, metric)
    if not usable:
        raise ValueError(f"no cataloged runs carry the metric {metric!r}")
    grand_mean = _mean(tuple(value for _, value in usable))
    names: set[str] = set()
    for point, _ in usable:
        names.update(point.parameters)
    parameters = tuple(
        _parameter_sensitivity(name, usable, grand_mean) for name in sorted(names)
    )
    return SweepSensitivity(
        metric=metric,
        runs_used=len(usable),
        grand_mean=grand_mean,
        parameters=parameters,
    )


def _parameter_sensitivity(
    name: str,
    usable: tuple[tuple[SweepPoint, Decimal], ...],
    grand_mean: Decimal,
) -> ParameterSensitivity:
    grouped: dict[str, list[Decimal]] = {}
    for point, value in usable:
        if name in point.parameters:
            grouped.setdefault(_level_text(point.parameters[name]), []).append(value)
    levels = tuple(
        ParameterLevelMean(value=level, mean=_mean(tuple(values)), count=len(values))
        for level, values in sorted(grouped.items())
    )
    effect_range = max(level.mean for level in levels) - min(
        level.mean for level in levels
    )
    best = min(levels, key=lambda level: (-level.mean, level.value))
    return ParameterSensitivity(
        name=name,
        levels=levels,
        grand_mean=grand_mean,
        effect_range=effect_range,
        best_value=best.value,
    )


__all__ = [
    "ParameterLevelMean",
    "ParameterSensitivity",
    "RankedRun",
    "SweepSensitivity",
    "parameter_sensitivity",
    "rank_runs",
]
