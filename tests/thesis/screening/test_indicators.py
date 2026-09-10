"""Indicator bridge tests over canned daily OHLCV fixtures.

Every expectation is hand-computed from the canned bars so the bridge's
synthetic replay (close series, bar ranges, dollar notional) is pinned to
exact registry semantics rather than approximations.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from thesis.screening.indicators import (
    IndicatorBridgeError,
    OhlcvBar,
    clip_bars,
    compute_indicator_value,
    indicator_warmup_days,
)

SYMBOL = "AAA"
START = date(2026, 8, 1)


def _bar(day: date, close: str, volume: str = "1000") -> OhlcvBar:
    return OhlcvBar(
        bar_date=day,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def _day(index: int) -> date:
    return date.fromordinal(START.toordinal() + index)


def _flat_bars(closes: list[str]) -> tuple[OhlcvBar, ...]:
    return tuple(_bar(_day(index), close) for index, close in enumerate(closes))


def test_clip_bars_hides_bars_after_the_window_end() -> None:
    bars = (
        _bar(date(2026, 9, 8), "10.00"),
        _bar(date(2026, 9, 9), "11.00"),
        _bar(date(2026, 9, 10), "99.00"),
    )

    visible = clip_bars(
        bars, window_start=date(2026, 9, 1), window_end=date(2026, 9, 9)
    )

    assert [bar.bar_date for bar in visible] == [date(2026, 9, 8), date(2026, 9, 9)]


def test_clip_bars_rejects_duplicate_dates() -> None:
    bars = (_bar(_day(0), "10.00"), _bar(_day(0), "11.00"))

    with pytest.raises(IndicatorBridgeError, match="duplicate"):
        clip_bars(bars, window_start=START, window_end=_day(1))


def test_rsi_pure_uptrend_reads_one_hundred() -> None:
    closes = [f"{10 + index}.00" for index in range(20)]
    value = compute_indicator_value(
        "rsi",
        {"period": 14},
        SYMBOL,
        _flat_bars(closes),
        window_start=START,
        window_end=_day(19),
    )

    assert value is not None
    assert float(value) == 100.0


def test_rsi_mixed_series_stays_between_zero_and_hundred() -> None:
    closes = ["10", "11", "9", "12", "8", "13", "7", "14", "6", "15"]
    value = compute_indicator_value(
        "rsi",
        {"period": 5},
        SYMBOL,
        _flat_bars(closes),
        window_start=START,
        window_end=_day(len(closes) - 1),
    )

    assert value is not None
    assert 0.0 <= float(value) <= 100.0


def test_rsi_pure_downtrend_reads_zero() -> None:
    closes = [f"{20 - index}.00" for index in range(20)]
    value = compute_indicator_value(
        "rsi",
        {"period": 14},
        SYMBOL,
        _flat_bars(closes),
        window_start=START,
        window_end=_day(19),
    )

    assert value is not None
    assert float(value) == 0.0


def test_sma_is_the_rolling_mean_of_the_last_period_closes() -> None:
    closes = ["10.00", "11.00", "12.00", "13.00", "14.00"]
    value = compute_indicator_value(
        "sma",
        {"period": 3},
        SYMBOL,
        _flat_bars(closes),
        window_start=START,
        window_end=_day(4),
    )

    assert value is not None
    assert float(value) == pytest.approx(13.0)


def test_atr_uses_exact_daily_true_ranges() -> None:
    bars = (
        OhlcvBar(
            bar_date=_day(0),
            open=Decimal("10.0"),
            high=Decimal("12.0"),
            low=Decimal("9.0"),
            close=Decimal("11.0"),
            volume=Decimal("1000"),
        ),
        OhlcvBar(
            bar_date=_day(1),
            open=Decimal("11.0"),
            high=Decimal("13.0"),
            low=Decimal("10.5"),
            close=Decimal("12.5"),
            volume=Decimal("1000"),
        ),
        OhlcvBar(
            bar_date=_day(2),
            open=Decimal("12.5"),
            high=Decimal("14.0"),
            low=Decimal("11.0"),
            close=Decimal("11.5"),
            volume=Decimal("1000"),
        ),
    )
    # True ranges: day0 (12-9)=3.0; day1 max(13-10.5, 13-12.5, 12.5-11)=2.5;
    # day2 max(14-11, |14-12.5|, |11-12.5|)=3.0.
    value = compute_indicator_value(
        "atr",
        {"period": 3},
        SYMBOL,
        bars,
        window_start=START,
        window_end=_day(2),
    )

    assert value is not None
    assert float(value) == pytest.approx((3.0 + 2.5 + 3.0) / 3.0, abs=1e-9)


def test_dollar_volume_accumulates_the_exact_notional() -> None:
    bars = tuple(
        _bar(_day(index), "10.00", volume=f"{1000 * (index + 1)}") for index in range(5)
    )

    value = compute_indicator_value(
        "dollar_volume",
        {},
        SYMBOL,
        bars,
        window_start=START,
        window_end=_day(4),
    )

    assert value is not None
    assert float(value) == pytest.approx(10.0 * 1000 * 15)


def test_indicator_value_is_none_before_warmup() -> None:
    value = compute_indicator_value(
        "sma",
        {"period": 5},
        SYMBOL,
        _flat_bars(["10.00", "11.00"]),
        window_start=START,
        window_end=_day(1),
    )
    assert value is None


def test_warmup_days_follow_the_indicator_params() -> None:
    assert indicator_warmup_days("sma", {"period": 20}) == 21
    assert indicator_warmup_days("rsi", {"period": 14}) == 15
    assert indicator_warmup_days("atr", {"period": 14}) == 15
    assert indicator_warmup_days("dollar_volume", {}) == 0
    assert indicator_warmup_days("dollar_volume", {"window": "30d"}) == 30


def test_ohlcv_bar_rejects_inconsistent_values() -> None:
    with pytest.raises(ValueError, match="reconcile"):
        OhlcvBar(
            bar_date=date(2026, 9, 9),
            open=Decimal("12.00"),
            high=Decimal("10.00"),
            low=Decimal("9.00"),
            close=Decimal("11.00"),
            volume=Decimal("1"),
        )


def test_window_end_is_a_hard_point_in_time_boundary() -> None:
    closes = [f"{10 + index}.00" for index in range(10)]
    base = date.fromordinal(date(2026, 8, 25).toordinal())
    bars = tuple(
        _bar(date.fromordinal(base.toordinal() + index), close)
        for index, close in enumerate(closes)
    ) + (_bar(date(2026, 9, 10), "999.00"),)

    value = compute_indicator_value(
        "sma",
        {"period": 3},
        SYMBOL,
        bars,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 9),
    )

    assert value is not None
    # Only bars through 2026-09-09 are visible: the last three closes are
    # 17, 18, and 19, and the 999 close on 2026-09-10 is invisible.
    assert float(value) == pytest.approx(18.0)


def test_ohlcv_bar_dollar_volume_is_exact() -> None:
    bar = OhlcvBar(
        bar_date=date(2026, 9, 9),
        open=Decimal("10.00"),
        high=Decimal("10.50"),
        low=Decimal("10.00"),
        close=Decimal("10.50"),
        volume=Decimal("4"),
    )
    assert bar.dollar_volume == Decimal("42.00")
