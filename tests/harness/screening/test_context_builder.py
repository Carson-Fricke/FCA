"""Context builder: payload flattening and host-side indicator computation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from harness.screening import (
    BarRecord,
    FilterContext,
    FilterErrorKind,
    FilterEvaluationError,
    IndicatorSeriesRequest,
    SecurityFacts,
    compute_indicator_series,
    evaluate_filter,
)


def test_payload_shape_is_plain_json_data(make_context) -> None:
    payload = make_context(symbol="AAPL").to_payload()
    assert payload["facts"] == {
        "symbol": "AAPL",
        "security_type": "common_stock",
        "exchange": "NASDAQ",
        "sic_industry": "Electronic Computers",
        "as_of": "2024-05-31",
        "fundamentals": {"market_cap": 1.0e12, "active": True},
    }
    bars = payload["bars"]
    assert bars[0] == {
        "date": "2024-05-01",
        "open": 169.0,
        "high": 172.0,
        "low": 168.0,
        "close": 170.0,
        "volume": 1_000_000,
    }
    indicators = payload["indicators"]
    assert set(indicators) == {"sma_5"}
    assert indicators["sma_5"][0] is None
    assert indicators["sma_5"][-1] == 178.6


def test_indicator_series_helper_matches_hand_computation(make_bars) -> None:
    bars = make_bars(("10", "20", "30", "40"))
    series = compute_indicator_series(
        "AAPL", bars, (IndicatorSeriesRequest("sma", {"period": 2}),)
    )
    values = series["sma(period=2)"]
    assert values == (None, 15.0, 25.0, 35.0)


def test_indicator_series_alias_is_used(make_bars) -> None:
    bars = make_bars(("10", "20", "30"))
    series = compute_indicator_series(
        "AAPL", bars, (IndicatorSeriesRequest("sma", {"period": 2}, alias="fast"),)
    )
    assert set(series) == {"fast"}


def test_indicator_series_empty_window(make_bars) -> None:
    series = compute_indicator_series(
        "AAPL", (), (IndicatorSeriesRequest("sma", {"period": 2}),)
    )
    assert series == {"sma(period=2)": ()}


def test_indicator_series_unknown_indicator_fails(make_bars) -> None:
    bars = make_bars(("10", "20"))
    with pytest.raises(Exception, match="unknown indicator"):
        compute_indicator_series("AAPL", bars, (IndicatorSeriesRequest("nope", {}),))


def test_indicator_series_rejects_unordered_bars(make_bars) -> None:
    bars = make_bars(("10", "20", "30"))
    with pytest.raises(ValueError, match="strictly ascending"):
        compute_indicator_series(
            "AAPL",
            (bars[1], bars[0]),
            (IndicatorSeriesRequest("sma", {"period": 2}),),
        )


def test_facts_reject_invalid_fields(make_facts) -> None:
    with pytest.raises(ValueError):
        make_facts(symbol="")
    with pytest.raises(ValueError):
        make_facts(security_type="")
    with pytest.raises(ValueError):
        SecurityFacts(
            "AAPL",
            "common_stock",
            "NASDAQ",
            "x",
            "2024-05-31",  # type: ignore[arg-type]
            {},
        )


def test_facts_reject_non_scalar_fundamentals(make_facts) -> None:
    with pytest.raises(TypeError):
        make_facts(fundamentals={"nested": {"a": 1}})
    with pytest.raises(ValueError):
        make_facts(fundamentals={"cap": float("nan")})


def test_bar_records_reject_bad_geometry(make_bars) -> None:
    with pytest.raises(ValueError):
        BarRecord(
            date(2024, 5, 1), Decimal("5"), Decimal("4"), Decimal("1"), Decimal("3"), 10
        )
    with pytest.raises(ValueError):
        BarRecord(
            date(2024, 5, 1), Decimal("1"), Decimal("9"), Decimal("2"), Decimal("3"), 10
        )
    with pytest.raises(ValueError):
        BarRecord(
            date(2024, 5, 1), Decimal("1"), Decimal("9"), Decimal("2"), Decimal("3"), 0
        )
    with pytest.raises(ValueError):
        BarRecord(
            date(2024, 5, 1),
            Decimal("-1"),
            Decimal("9"),
            Decimal("-2"),
            Decimal("3"),
            10,
        )


def test_filter_context_rejects_misaligned_series(make_facts, make_bars) -> None:
    with pytest.raises(ValueError):
        FilterContext(make_facts(), make_bars(), {"sma_5": (1.0,)})
    with pytest.raises(TypeError):
        FilterContext(make_facts(), make_bars(), {"sma": (True, None)})
    with pytest.raises(ValueError):
        FilterContext(make_facts(), make_bars(), {"sma": (float("inf"),) * 10})


def test_filter_context_rejects_unordered_dates(make_facts, make_bars) -> None:
    bars = make_bars()
    with pytest.raises(ValueError):
        FilterContext(make_facts(), (bars[1], bars[0]), ())


def test_evaluation_round_trips_indicator_series_to_the_sandbox(make_context) -> None:
    script = """
def screen(ctx):
    sma = ctx["indicators"]["sma_5"]
    warm = [value for value in sma if value is not None]
    if not warm:
        return False
    return warm == [171.0, 172.0, 172.6, 174.0, 175.2, 178.6]
"""
    result = evaluate_filter(script, make_context("AAPL"))
    assert result.passed is True


def test_error_records_render_with_hash(make_context) -> None:
    with pytest.raises(FilterEvaluationError) as raised:
        evaluate_filter(
            "import os\n\ndef screen(ctx):\n    return True", make_context()
        )
    record = raised.value.error
    assert record.kind is FilterErrorKind.IMPORT_BLOCKED
    assert record.render().startswith("import-blocked: ")
    assert record.script_hash
