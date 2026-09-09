"""Preset screener filters with deterministic evaluation semantics.

Every filter is a pure function of the per-symbol metric mapping (plus the
candidate-set percentile rank for relative filters), so a screen's outcome
depends only on the injected data and the declaration order. Presets never
touch ports, wall clocks, or randomness; parameters are validated at
declaration time and every violation raises :class:`FilterError`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from thesis.screening.engine import ScreenerError


class FilterError(ScreenerError):
    """A preset filter was declared with invalid parameters."""


@dataclass(frozen=True, slots=True)
class FilterEvaluator:
    """One resolved, ready-to-apply filter instance."""

    kind: str
    params: Mapping[str, object]
    predicate: Callable[[Mapping[str, object], float], bool]

    def evaluate(self, metrics: Mapping[str, object], *, percentile: float) -> bool:
        """Return whether the symbol's metrics pass this filter."""
        try:
            return self.predicate(metrics, percentile)
        except (TypeError, ValueError, KeyError, ZeroDivisionError) as error:
            raise FilterError(
                f"filter {self.kind!r} could not evaluate the metrics: {error}"
            ) from error


Validator = Callable[[Mapping[str, object]], Mapping[str, object]]
Predicate = Callable[[Mapping[str, object], float], bool]


def _require_number(params: Mapping[str, object], key: str) -> float:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FilterError(f"{key} must be a number")
    number = float(value)
    if number < 0:
        raise FilterError(f"{key} must not be negative")
    return number


def _require_fraction(params: Mapping[str, object], key: str) -> float:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FilterError(f"{key} must be a number")
    fraction = float(value)
    if not 0.0 <= fraction <= 1.0:
        raise FilterError(f"{key} must be within [0, 1]")
    return fraction


def _require_non_negative_int(params: Mapping[str, object], key: str) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FilterError(f"{key} must be a non-negative integer")
    return value


def _avg_dollar_volume_min(
    params: Mapping[str, object],
) -> tuple[Mapping[str, object], Predicate]:
    threshold = _require_number(params, "min")

    def predicate(metrics: Mapping[str, object], percentile: float) -> bool:
        raw = metrics.get("avg_dollar_volume")
        if not isinstance(raw, str):
            raise FilterError("avg_dollar_volume metric must be a string")
        return float(raw) >= threshold

    return {"min": threshold}, predicate


def _min_price(params: Mapping[str, object]) -> tuple[Mapping[str, object], Predicate]:
    threshold = _require_number(params, "min")

    def predicate(metrics: Mapping[str, object], percentile: float) -> bool:
        raw = metrics.get("min_close")
        if not isinstance(raw, str):
            raise FilterError("min_close metric must be a string")
        return float(raw) >= threshold

    return {"min": threshold}, predicate


def _listing_age_days_min(
    params: Mapping[str, object],
) -> tuple[Mapping[str, object], Predicate]:
    minimum = _require_non_negative_int(params, "min_days")

    def predicate(metrics: Mapping[str, object], percentile: float) -> bool:
        raw = metrics.get("listing_age_days")
        if not isinstance(raw, str) or not raw.lstrip("-").isdigit():
            raise FilterError("listing_age_days metric must be an integer string")
        return int(raw) >= minimum

    return {"min_days": minimum}, predicate


def _avg_volume_percentile_min(
    params: Mapping[str, object],
) -> tuple[Mapping[str, object], Predicate]:
    minimum = _require_fraction(params, "min")

    def predicate(metrics: Mapping[str, object], percentile: float) -> bool:
        return percentile >= minimum

    return {"min": minimum}, predicate


_PRESETS: Final[
    dict[str, Callable[[Mapping[str, object]], tuple[Mapping[str, object], Predicate]]]
] = {
    "avg_dollar_volume_min": _avg_dollar_volume_min,
    "min_price": _min_price,
    "listing_age_days_min": _listing_age_days_min,
    "avg_volume_percentile_min": _avg_volume_percentile_min,
}


def resolve_filter(kind: object, params: Mapping[str, object]) -> FilterEvaluator:
    """Resolve one declared filter kind with validated parameters.

    The returned evaluator closes over its validated parameters, so applying
    it never re-reads unvalidated declarations. Unknown kinds and invalid
    parameters raise :class:`FilterError` at declaration time.
    """
    if not isinstance(kind, str) or not kind:
        raise FilterError("filter kind must be a non-empty string")
    if kind not in _PRESETS:
        known = ", ".join(sorted(_PRESETS))
        raise FilterError(f"unknown filter kind {kind!r}; known: {known}")
    params_mapping: Mapping[str, object] = params if isinstance(params, Mapping) else {}
    validated, predicate = _PRESETS[kind](params_mapping)
    return FilterEvaluator(kind=kind, params=validated, predicate=predicate)


def and_filters(*evaluators: FilterEvaluator) -> FilterEvaluator:
    """Compose filters conjunctively in declaration order."""

    if not evaluators:
        raise FilterError("and_filters requires at least one filter")

    def predicate(metrics: Mapping[str, object], percentile: float) -> bool:
        return all(
            evaluator.evaluate(metrics, percentile=percentile)
            for evaluator in evaluators
        )

    return FilterEvaluator(kind="and", params={}, predicate=predicate)
