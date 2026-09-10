"""Declarative filter vocabulary tests: classification, fundamentals, and
indicator conditions evaluated over canned fact sheets."""

from __future__ import annotations

import pytest

from thesis.screening import (
    FilterError,
    and_filters,
    indicator_metric_key,
    resolve_filter,
)

IND_RSI = "ind:rsi(period=14)"
IND_SMA = "ind:sma(period=20)"
IND_ATR = "ind:atr(period=14)"
IND_DOLLAR_VOLUME = "ind:dollar_volume()"


def _facts(**overrides: object) -> dict[str, str]:
    base = {
        "avg_dollar_volume": "10000000.0",
        "min_close": "9.0",
        "last_close": "12.0",
        "listing_age_days": "400",
        "avg_volume": "1000000.0",
        "security_type": "stock",
        "exchange": "NASDAQ",
        "sic_code": "3571",
        "sic_industry": "Electronic Computers",
        "shares_outstanding": "1000000.0",
        "revenue": "250000000.0",
        "net_income": "-5000.0",
        "eps": "1.5",
    }
    base.update({key: str(value) for key, value in overrides.items()})
    return base


# --------------------------------------------------------------------------
# Classification filters
# --------------------------------------------------------------------------


def test_security_type_filter_matches_case_insensitively() -> None:
    etf = resolve_filter("security_type", {"value": "etf"})
    assert etf.evaluate(_facts(security_type="ETF"), percentile=0.5)
    assert not etf.evaluate(_facts(), percentile=0.5)
    assert not etf.evaluate(_facts(), percentile=0.5, ranks={})


def test_security_type_rejects_unknown_values_at_declaration() -> None:
    with pytest.raises(FilterError, match="must be one of"):
        resolve_filter("security_type", {"value": "future"})


def test_exchange_filter_matches_the_listing_exchange() -> None:
    nasdaq = resolve_filter("exchange", {"value": "nasdaq"})
    assert nasdaq.evaluate(_facts(), percentile=0.5)
    assert not nasdaq.evaluate(_facts(exchange="NYSE"), percentile=0.5)
    assert not nasdaq.evaluate({}, percentile=0.5)


def test_sic_industry_matches_by_exact_code() -> None:
    code = resolve_filter("sic_industry", {"code": "3571"})
    assert code.evaluate(_facts(), percentile=0.5)
    assert not code.evaluate(_facts(sic_code="3674"), percentile=0.5)
    assert not code.evaluate({}, percentile=0.5)


def test_sic_industry_matches_by_description_prefix() -> None:
    prefix = resolve_filter("sic_industry", {"prefix": "electronic comp"})
    assert prefix.evaluate(_facts(), percentile=0.5)
    assert not prefix.evaluate(_facts(sic_industry="Pharmaceuticals"), percentile=0.5)
    assert not prefix.evaluate({}, percentile=0.5)


def test_sic_industry_requires_exactly_one_matcher() -> None:
    with pytest.raises(FilterError, match="exactly one"):
        resolve_filter("sic_industry", {})
    with pytest.raises(FilterError, match="exactly one"):
        resolve_filter("sic_industry", {"code": "3571", "prefix": "Electronic"})
    with pytest.raises(FilterError, match="numeric SIC code"):
        resolve_filter("sic_industry", {"code": "computers"})


# --------------------------------------------------------------------------
# Fundamentals filters (point-in-time values, may be negative)
# --------------------------------------------------------------------------


def test_fundamentals_min_and_max_read_the_fact_sheet() -> None:
    revenue_min = resolve_filter(
        "fundamentals_min", {"field": "revenue", "min": 100_000_000}
    )
    shares_max = resolve_filter(
        "fundamentals_max", {"field": "shares_outstanding", "max": 2_000_000}
    )
    assert revenue_min.evaluate(_facts(), percentile=0.5)
    assert not revenue_min.evaluate(_facts(revenue=50_000_000), percentile=0.5)
    assert not revenue_min.evaluate({}, percentile=0.5)
    assert shares_max.evaluate(_facts(), percentile=0.5)


def test_fundamentals_bounds_accept_negative_thresholds() -> None:
    profitable = resolve_filter("fundamentals_min", {"field": "net_income", "min": 0})
    lossmaking = resolve_filter("fundamentals_max", {"field": "net_income", "max": 0})
    assert not profitable.evaluate(_facts(), percentile=0.5)
    assert lossmaking.evaluate(_facts(), percentile=0.5)
    assert profitable.evaluate(_facts(net_income=2000.0), percentile=0.5)


def test_fundamentals_rejects_unknown_fields_and_missing_bounds() -> None:
    with pytest.raises(FilterError, match="must be one of"):
        resolve_filter("fundamentals_min", {"field": "goodwill", "min": 1})
    with pytest.raises(FilterError, match="must be a number"):
        resolve_filter("fundamentals_min", {"field": "revenue"})


# --------------------------------------------------------------------------
# Indicator conditions
# --------------------------------------------------------------------------


def test_rsi_below_and_above_compare_the_precomputed_value() -> None:
    oversold = resolve_filter("rsi_below", {"period": 14, "threshold": 30})
    overbought = resolve_filter("rsi_above", {"period": 14, "threshold": 70})

    assert oversold.evaluate(_facts(**{IND_RSI: "22.5"}), percentile=0.5)
    assert not overbought.evaluate(_facts(**{IND_RSI: "22.5"}), percentile=0.5)
    assert overbought.evaluate(_facts(**{IND_RSI: "75.0"}), percentile=0.5)
    # A missing indicator value fails the symbol deterministically.
    assert not overbought.evaluate(_facts(), percentile=0.5)


def test_price_sma_filters_compare_last_close_with_the_overlay() -> None:
    above = resolve_filter("price_above_sma", {"period": 20})
    below = resolve_filter("price_below_sma", {"period": 20})

    assert above.evaluate(_facts(**{IND_SMA: "11.0"}), percentile=0.5)
    assert below.evaluate(_facts(**{IND_SMA: "13.0"}), percentile=0.5)
    assert not above.evaluate(_facts(**{IND_SMA: "13.0"}), percentile=0.5)
    assert not above.evaluate(_facts(), percentile=0.5)


def test_indicator_percentile_filters_use_candidate_set_ranks() -> None:
    volatile = resolve_filter("atr_percentile_min", {"period": 14, "min": 0.8})
    liquid = resolve_filter("dollar_volume_percentile_min", {"min": 0.9})

    ranks = {IND_ATR: 0.9, IND_DOLLAR_VOLUME: 0.5}
    assert volatile.evaluate(_facts(), percentile=0.1, ranks=ranks)
    assert not volatile.evaluate(_facts(), percentile=0.1, ranks={IND_ATR: 0.4})
    assert not liquid.evaluate(_facts(), percentile=0.1, ranks=ranks)


def test_dollar_volume_percentile_declares_its_window() -> None:
    cumulative = resolve_filter("dollar_volume_percentile_min", {"min": 0.5})
    assert cumulative.indicators[0].name == "dollar_volume"
    assert dict(cumulative.indicators[0].params) == {}

    rolling = resolve_filter(
        "dollar_volume_percentile_min", {"min": 0.5, "window_days": 30}
    )
    assert rolling.indicators[0].params == {"window": "30d"}
    with pytest.raises(FilterError, match="positive integer"):
        resolve_filter("dollar_volume_percentile_min", {"min": 0.5, "window_days": 0})


# --------------------------------------------------------------------------
# Composition and declaration-time validation
# --------------------------------------------------------------------------


def test_and_filters_composes_the_new_families() -> None:
    composed = and_filters(
        resolve_filter("security_type", {"value": "stock"}),
        resolve_filter("exchange", {"value": "NASDAQ"}),
        resolve_filter("rsi_below", {"period": 14, "threshold": 30}),
    )
    assert composed.evaluate(_facts(**{IND_RSI: "25.0"}), percentile=0.5, ranks={})
    assert not composed.evaluate(
        _facts(security_type="etf", **{IND_RSI: "25.0"}), percentile=0.5, ranks={}
    )
    assert composed.indicators[0].name == "rsi"
    assert set(composed.fact_sources) == {"classification"}


def test_indicator_filters_declare_their_registry_requests() -> None:
    rsi = resolve_filter("rsi_below", {"period": 14, "threshold": 30})
    assert rsi.indicators[0].name == "rsi"
    assert rsi.indicators[0].metric_key == IND_RSI
    sma = resolve_filter("price_above_sma", {"period": 20})
    assert sma.indicators[0].metric_key == IND_SMA
    atr = resolve_filter("atr_percentile_min", {"period": 14, "min": 0.5})
    assert atr.indicators[0].metric_key == IND_ATR


def test_indicator_parameters_fail_at_declaration() -> None:
    with pytest.raises(FilterError, match="positive integer"):
        resolve_filter("rsi_below", {"period": 0, "threshold": 30})
    with pytest.raises(FilterError, match="within"):
        resolve_filter("rsi_above", {"period": 14, "threshold": 150})
    with pytest.raises(FilterError, match="threshold"):
        resolve_filter("rsi_above", {"period": 14})


def test_indicator_metric_key_is_canonical() -> None:
    assert indicator_metric_key("rsi", {"period": 14}) == IND_RSI
    assert indicator_metric_key("dollar_volume", {}) == IND_DOLLAR_VOLUME
