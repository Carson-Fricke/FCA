"""Shared builders for the screening sandbox test suite."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import TypedDict, Unpack

import pytest

from harness.screening import (
    BarRecord,
    FilterContext,
    IndicatorSeriesRequest,
    SecurityFacts,
    compute_indicator_series,
)

BASE_DATE: date = date(2024, 5, 1)
AS_OF: date = date(2024, 5, 31)

GOLDEN_SCRIPT = """
import math


def screen(ctx):
    facts = ctx["facts"]
    bars = ctx["bars"]
    if not bars or facts["security_type"] != "common_stock":
        return False
    sma = ctx["indicators"].get("sma_5")
    if sma is None or sma[-1] is None:
        return False
    last = bars[-1]["close"]
    average = sma[-1]
    premium = (last - average) / average
    return premium > 0.02 and math.isfinite(premium)
"""

AAPL_CLOSES: tuple[str, ...] = (
    "170",
    "171",
    "169",
    "172",
    "173",
    "175",
    "174",
    "176",
    "178",
    "190",
)

SMA_REQUESTS: tuple[IndicatorSeriesRequest, ...] = (
    IndicatorSeriesRequest("sma", {"period": 5}, alias="sma_5"),
)


def build_facts(
    symbol: str = "AAPL",
    security_type: str = "common_stock",
    exchange: str = "NASDAQ",
    sic_industry: str = "Electronic Computers",
    as_of: date = AS_OF,
    fundamentals: Mapping[str, str | int | float | bool | None] | None = None,
) -> SecurityFacts:
    """Build one security fact sheet with deterministic defaults."""
    return SecurityFacts(
        symbol=symbol,
        security_type=security_type,
        exchange=exchange,
        sic_industry=sic_industry,
        as_of=as_of,
        fundamentals=dict(fundamentals or {"market_cap": 1.0e12, "active": True}),
    )


def build_bars(
    closes: Sequence[str] = AAPL_CLOSES,
    *,
    start: date = BASE_DATE,
    base_volume: int = 1_000_000,
) -> tuple[BarRecord, ...]:
    """Build a deterministic ascending OHLCV window from close prices."""
    return tuple(
        BarRecord(
            bar_date=start + timedelta(days=index),
            open=Decimal(close) - 1,
            high=Decimal(close) + 2,
            low=Decimal(close) - 2,
            close=Decimal(close),
            volume=base_volume + index * 1000,
        )
        for index, close in enumerate(closes)
    )


class _FactOverrides(TypedDict, total=False):
    """Keyword overrides forwarded to :func:`build_facts` (minus ``symbol``)."""

    security_type: str
    exchange: str
    sic_industry: str
    as_of: date
    fundamentals: Mapping[str, str | int | float | bool | None]


def build_context(
    symbol: str = "AAPL",
    closes: Sequence[str] = AAPL_CLOSES,
    indicators: Mapping[str, tuple[float | None, ...]] | None = None,
    **fact_overrides: Unpack[_FactOverrides],
) -> FilterContext:
    """Build one screening context with a host-computed sma_5 series."""
    bars = build_bars(tuple(closes))
    series = dict(indicators or {})
    if not series:
        series.update(
            compute_indicator_series(
                symbol,
                bars,
                (IndicatorSeriesRequest("sma", {"period": 5}, alias="sma_5"),),
            )
        )
    facts = build_facts(symbol, **fact_overrides)
    return FilterContext(facts, bars, series)


@pytest.fixture
def make_facts() -> Callable[..., SecurityFacts]:
    """Return the deterministic fact-sheet builder."""
    return build_facts


@pytest.fixture
def make_bars() -> Callable[..., tuple[BarRecord, ...]]:
    """Return the deterministic OHLCV window builder."""
    return build_bars


@pytest.fixture
def make_context() -> Callable[..., FilterContext]:
    """Return the deterministic screening-context builder."""
    return build_context


@pytest.fixture
def golden_script() -> str:
    """Return the legitimate facts-plus-indicator golden script."""
    return GOLDEN_SCRIPT
