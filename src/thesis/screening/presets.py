"""Preset screener filters with deterministic evaluation semantics.

Every filter is a pure function of the per-symbol fact sheet (plus the
candidate-set percentile ranks for relative filters), so a screen's outcome
depends only on the injected data and the declaration order. Presets never
touch ports, wall clocks, or randomness; parameters are validated at
declaration time and every violation raises :class:`FilterError`.

The filter vocabulary spans four families:

- market metrics (the T219 volume/price/age presets);
- classification facts (``security_type``, ``exchange``, ``sic_industry``),
  sourced point-in-time from the context's classification port;
- fundamentals facts (``shares_outstanding``, ``revenue``, ``net_income``,
  ``eps``), resolved through the context's filed-date point-in-time
  fundamentals port;
- technical conditions computed through the harness indicator registry over
  the OHLCV window at the as-of instant (RSI, SMA price overlays, ATR and
  dollar-volume candidate-set percentiles).  Scripts and filters never
  hand-roll the math: every indicator value arrives precomputed under its
  canonical ``ind:<name>(<params>)`` fact-sheet key.

A fact absent from the fact sheet (no classification row, no visible
fundamental, an indicator that never warmed up) fails the filters that read
it deterministically; it is a screened-out symbol, not an error.  A filter
whose required port the context does not expose at all fails closed in the
engine with :class:`~thesis.screening.engine.ScreenerPortError`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from thesis.screening.engine import ScreenerError


class FilterError(ScreenerError):
    """A preset filter was declared with invalid parameters."""


SECURITY_TYPES: Final[tuple[str, ...]] = ("stock", "etf")
FUNDAMENTAL_FIELDS: Final[tuple[str, ...]] = (
    "shares_outstanding",
    "revenue",
    "net_income",
    "eps",
)


@dataclass(frozen=True, slots=True)
class IndicatorRequest:
    """One declarative indicator request resolved through the registry."""

    name: str
    params: Mapping[str, object] = field(default_factory=dict)

    @property
    def metric_key(self) -> str:
        """Return the canonical fact-sheet key for this request's value."""
        return indicator_metric_key(self.name, self.params)


def indicator_metric_key(name: str, params: Mapping[str, object]) -> str:
    """Return the canonical fact-sheet key for one indicator request."""
    rendered = ",".join(f"{key}={params[key]}" for key in sorted(params, key=str))
    return f"ind:{name}({rendered})"


@dataclass(frozen=True, slots=True)
class FilterEvaluator:
    """One resolved, ready-to-apply filter instance.

    ``indicators`` lists the indicator requests the engine must precompute
    before evaluation, and ``fact_sources`` names the optional context ports
    (``classification``/``fundamentals``) the filter's facts come from, so
    the engine can fail closed when a context cannot supply them.
    """

    kind: str
    params: Mapping[str, object]
    predicate: Predicate
    indicators: tuple[IndicatorRequest, ...] = ()
    fact_sources: tuple[str, ...] = ()

    def evaluate(
        self,
        metrics: Mapping[str, object],
        *,
        percentile: float,
        ranks: Mapping[str, float] | None = None,
    ) -> bool:
        """Return whether the symbol's fact sheet passes this filter."""
        try:
            return self.predicate(metrics, percentile, ranks or {})
        except FilterError:
            raise
        except (TypeError, ValueError, KeyError, ZeroDivisionError) as error:
            raise FilterError(
                f"filter {self.kind!r} could not evaluate the fact sheet: {error}"
            ) from error


Validator = Callable[[Mapping[str, object]], Mapping[str, object]]
Predicate = Callable[[Mapping[str, object], float, Mapping[str, float]], bool]
FilterFactory = Callable[
    [Mapping[str, object]],
    tuple[
        Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
    ],
]


def _require_number(params: Mapping[str, object], key: str) -> float:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FilterError(f"{key} must be a number")
    number = float(value)
    if number < 0:
        raise FilterError(f"{key} must not be negative")
    return number


def _require_signed_number(params: Mapping[str, object], key: str) -> float:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FilterError(f"{key} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise FilterError(f"{key} must be finite")
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


def _require_positive_int(params: Mapping[str, object], key: str) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FilterError(f"{key} must be a positive integer")
    return value


def _require_choice(
    params: Mapping[str, object], key: str, choices: tuple[str, ...]
) -> str:
    value = params.get(key)
    if not isinstance(value, str) or value.strip().lower() not in choices:
        known = ", ".join(choices)
        raise FilterError(f"{key} must be one of: {known}")
    return value.strip().lower()


def _require_text(params: Mapping[str, object], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FilterError(f"{key} must be a non-empty string")
    return value.strip()


def _read_number(metrics: Mapping[str, object], key: str) -> float | None:
    """Read one numeric fact; ``None`` means the symbol does not carry it."""
    raw = metrics.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError as error:
            raise FilterError(f"{key} fact must be numeric: {raw!r}") from error
    if isinstance(raw, int | float):
        return float(raw)
    raise FilterError(f"{key} fact must be numeric: {raw!r}")


# --------------------------------------------------------------------------
# Market metric presets (T219 surface, unchanged semantics)
# --------------------------------------------------------------------------


def _avg_dollar_volume_min(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    threshold = _require_number(params, "min")

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del percentile, ranks
        raw = metrics.get("avg_dollar_volume")
        if not isinstance(raw, str):
            raise FilterError("avg_dollar_volume metric must be a string")
        return float(raw) >= threshold

    return {"min": threshold}, predicate, (), ()


def _min_price(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    threshold = _require_number(params, "min")

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del percentile, ranks
        raw = metrics.get("min_close")
        if not isinstance(raw, str):
            raise FilterError("min_close metric must be a string")
        return float(raw) >= threshold

    return {"min": threshold}, predicate, (), ()


def _listing_age_days_min(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    minimum = _require_non_negative_int(params, "min_days")

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del percentile, ranks
        raw = metrics.get("listing_age_days")
        if not isinstance(raw, str) or not raw.lstrip("-").isdigit():
            raise FilterError("listing_age_days metric must be an integer string")
        return int(raw) >= minimum

    return {"min_days": minimum}, predicate, (), ()


def _avg_volume_percentile_min(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    minimum = _require_fraction(params, "min")

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del metrics, ranks
        return percentile >= minimum

    return {"min": minimum}, predicate, (), ()


# --------------------------------------------------------------------------
# Classification facts
# --------------------------------------------------------------------------


def _security_type(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    value = _require_choice(params, "value", SECURITY_TYPES)

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del percentile, ranks
        raw = metrics.get("security_type")
        return isinstance(raw, str) and raw.strip().lower() == value

    return {"value": value}, predicate, (), ("classification",)


def _exchange(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    value = _require_text(params, "value").upper()

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del percentile, ranks
        raw = metrics.get("exchange")
        return isinstance(raw, str) and raw.strip().upper() == value

    return {"value": value}, predicate, (), ("classification",)


def _sic_industry(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    code = params.get("code")
    prefix = params.get("prefix")
    if (code is None) == (prefix is None):
        raise FilterError("sic_industry requires exactly one of code or prefix")
    if code is not None:
        if not isinstance(code, str) or not code.strip().isdigit():
            raise FilterError("code must be a numeric SIC code string")
        validated_code = code.strip()

        def predicate(
            metrics: Mapping[str, object],
            percentile: float,
            ranks: Mapping[str, float],
        ) -> bool:
            del percentile, ranks
            raw = metrics.get("sic_code")
            return isinstance(raw, str) and raw.strip() == validated_code

        return {"code": validated_code}, predicate, (), ("classification",)

    assert isinstance(prefix, str)  # exactly-one check above
    validated_prefix = _require_text({"prefix": prefix}, "prefix").lower()

    def predicate(
        metrics: Mapping[str, object],
        percentile: float,
        ranks: Mapping[str, float],
    ) -> bool:
        del percentile, ranks
        raw = metrics.get("sic_industry")
        return isinstance(raw, str) and raw.strip().lower().startswith(validated_prefix)

    return {"prefix": validated_prefix}, predicate, (), ("classification",)


# --------------------------------------------------------------------------
# Fundamentals facts (point-in-time via filed date)
# --------------------------------------------------------------------------


def _fundamentals_bound(params: Mapping[str, object], bound: str) -> tuple[str, float]:
    field = _require_choice(params, "field", FUNDAMENTAL_FIELDS)
    value = _require_signed_number(params, bound)
    return field, value


def _fundamentals_min(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    field, minimum = _fundamentals_bound(params, "min")

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del percentile, ranks
        value = _read_number(metrics, field)
        return value is not None and value >= minimum

    return {"field": field, "min": minimum}, predicate, (), ("fundamentals",)


def _fundamentals_max(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    field, maximum = _fundamentals_bound(params, "max")

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del percentile, ranks
        value = _read_number(metrics, field)
        return value is not None and value <= maximum

    return {"field": field, "max": maximum}, predicate, (), ("fundamentals",)


# --------------------------------------------------------------------------
# Indicator conditions (computed through the harness indicator registry)
# --------------------------------------------------------------------------


def _rsi_condition(
    params: Mapping[str, object], *, below: bool
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    period = _require_positive_int(params, "period")
    threshold = params.get("threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int | float)
        or not 0.0 <= float(threshold) <= 100.0
    ):
        raise FilterError("threshold must be a number within [0, 100]")
    bound = float(threshold)
    request = IndicatorRequest("rsi", {"period": period})
    key = request.metric_key

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del percentile, ranks
        value = _read_number(metrics, key)
        if value is None:
            return False
        return value <= bound if below else value >= bound

    return {"period": period, "threshold": bound}, predicate, (request,), ()


def _rsi_below(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    return _rsi_condition(params, below=True)


def _rsi_above(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    return _rsi_condition(params, below=False)


def _price_sma_condition(
    params: Mapping[str, object], *, above: bool
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    period = _require_positive_int(params, "period")
    request = IndicatorRequest("sma", {"period": period})
    key = request.metric_key

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del percentile, ranks
        price = _read_number(metrics, "last_close")
        sma = _read_number(metrics, key)
        if price is None or sma is None:
            return False
        return price >= sma if above else price <= sma

    return {"period": period}, predicate, (request,), ()


def _price_above_sma(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    return _price_sma_condition(params, above=True)


def _price_below_sma(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    return _price_sma_condition(params, above=False)


def _atr_percentile_min(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    period = _require_positive_int(params, "period")
    minimum = _require_fraction(params, "min")
    request = IndicatorRequest("atr", {"period": period})
    key = request.metric_key

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del metrics, percentile
        rank = ranks.get(key)
        return rank is not None and rank >= minimum

    return {"period": period, "min": minimum}, predicate, (request,), ()


def _dollar_volume_percentile_min(
    params: Mapping[str, object],
) -> tuple[
    Mapping[str, object], Predicate, tuple[IndicatorRequest, ...], tuple[str, ...]
]:
    minimum = _require_fraction(params, "min")
    window_days = params.get("window_days")
    if window_days is None:
        request = IndicatorRequest("dollar_volume", {})
        validated: Mapping[str, object] = {"min": minimum}
    else:
        days = _require_positive_int({"window_days": window_days}, "window_days")
        request = IndicatorRequest("dollar_volume", {"window": f"{days}d"})
        validated = {"min": minimum, "window_days": days}
    key = request.metric_key

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        del metrics, percentile
        rank = ranks.get(key)
        return rank is not None and rank >= minimum

    return validated, predicate, (request,), ()


_PRESETS: Final[dict[str, FilterFactory]] = {
    "avg_dollar_volume_min": _avg_dollar_volume_min,
    "min_price": _min_price,
    "listing_age_days_min": _listing_age_days_min,
    "avg_volume_percentile_min": _avg_volume_percentile_min,
    "security_type": _security_type,
    "exchange": _exchange,
    "sic_industry": _sic_industry,
    "fundamentals_min": _fundamentals_min,
    "fundamentals_max": _fundamentals_max,
    "rsi_below": _rsi_below,
    "rsi_above": _rsi_above,
    "price_above_sma": _price_above_sma,
    "price_below_sma": _price_below_sma,
    "atr_percentile_min": _atr_percentile_min,
    "dollar_volume_percentile_min": _dollar_volume_percentile_min,
}


def resolve_filter(kind: object, params: Mapping[str, object]) -> FilterEvaluator:
    """Resolve one declared filter kind with validated parameters.

    The returned evaluator closes over its validated parameters, the
    indicator requests the engine must precompute, and the optional fact
    ports it reads, so applying it never re-reads unvalidated declarations.
    Unknown kinds and invalid parameters raise :class:`FilterError` at
    declaration time.
    """
    if not isinstance(kind, str) or not kind:
        raise FilterError("filter kind must be a non-empty string")
    if kind not in _PRESETS:
        known = ", ".join(sorted(_PRESETS))
        raise FilterError(f"unknown filter kind {kind!r}; known: {known}")
    params_mapping: Mapping[str, object] = params if isinstance(params, Mapping) else {}
    validated, predicate, indicators, fact_sources = _PRESETS[kind](params_mapping)
    return FilterEvaluator(
        kind=kind,
        params=validated,
        predicate=predicate,
        indicators=indicators,
        fact_sources=fact_sources,
    )


def and_filters(*evaluators: FilterEvaluator) -> FilterEvaluator:
    """Compose filters conjunctively in declaration order."""

    if not evaluators:
        raise FilterError("and_filters requires at least one filter")

    def predicate(
        metrics: Mapping[str, object], percentile: float, ranks: Mapping[str, float]
    ) -> bool:
        return all(
            evaluator.evaluate(metrics, percentile=percentile, ranks=ranks)
            for evaluator in evaluators
        )

    indicators: list[IndicatorRequest] = []
    fact_sources: list[str] = []
    for evaluator in evaluators:
        for request in evaluator.indicators:
            if request not in indicators:
                indicators.append(request)
        for source in evaluator.fact_sources:
            if source not in fact_sources:
                fact_sources.append(source)
    return FilterEvaluator(
        kind="and",
        params={},
        predicate=predicate,
        indicators=tuple(indicators),
        fact_sources=tuple(fact_sources),
    )
