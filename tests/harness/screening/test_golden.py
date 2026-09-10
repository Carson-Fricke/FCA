"""Golden path: a legitimate facts-plus-indicator script through the sandbox.

The host computes the ``sma_5`` series through
``harness.indicators.create_indicator`` via ``compute_indicator_series``,
the sandbox consumes it as plain data, and the per-symbol booleans match
the hand-computed expectations.
"""

from __future__ import annotations

from harness.screening import (
    FilterBatchResult,
    evaluate_universe,
)

EXPECTED_GOLDEN_DECISIONS: dict[str, bool] = {
    "AAPL": True,
    "MSFT": False,
    "ETFA": False,
    "ZZTOP": False,
}


def test_golden_script_produces_expected_booleans(make_context, golden_script) -> None:
    universe = (
        make_context(symbol="AAPL"),
        make_context(symbol="MSFT", closes=("415",) * 10),
        make_context(symbol="ETFA", security_type="etf"),
        make_context(symbol="ZZTOP", closes=("12", "13", "12", "14")),
    )
    batch = evaluate_universe(golden_script, universe)
    assert isinstance(batch, FilterBatchResult)
    assert batch.decisions == EXPECTED_GOLDEN_DECISIONS
    assert batch.passed_symbols == ("AAPL",)
    assert batch.rejected_symbols == ("MSFT", "ETFA", "ZZTOP")
    assert batch.script_hash


def test_golden_series_values_are_the_hand_computed_means(make_context) -> None:
    context = make_context(symbol="AAPL")
    closes = (170, 171, 169, 172, 173, 175, 174, 176, 178, 190)
    sma = context.indicators["sma_5"]
    assert sma[:4] == (None, None, None, None)
    for index in range(4, 10):
        window = closes[index - 4 : index + 1]
        expected = sum(window) / 5
        assert sma[index] == expected


def test_golden_script_uses_facts_and_series_together(make_context) -> None:
    script = """
def screen(ctx):
    facts = ctx["facts"]
    if facts["sic_industry"] != "Electronic Computers":
        return False
    if not facts["fundamentals"]["active"]:
        return False
    sma = ctx["indicators"]["sma_5"]
    if sma is None or sma[-1] is None:
        return False
    return ctx["bars"][-1]["close"] > sma[-1]
"""
    universe = (
        make_context(symbol="AAPL"),
        make_context(symbol="DELL", sic_industry="Prepackaged Software"),
    )
    batch = evaluate_universe(script, universe)
    assert batch.decisions == {"AAPL": True, "DELL": False}


def test_golden_script_lints_clean(golden_script) -> None:
    from harness.screening import lint_filter_source

    report = lint_filter_source(golden_script)
    assert report.ok, report.render()
