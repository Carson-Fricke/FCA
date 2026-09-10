"""Sandbox parity: in-sandbox execution must equal the in-process reference.

Same script plus same data must produce identical decisions whether the
screen function runs through the subprocess protocol (the production path)
or in-process against restricted builtins (the reference executor), so the
process boundary and JSON marshalling change nothing observable.
"""

from __future__ import annotations

import pytest

from harness.screening import (
    FilterContext,
    FilterEvaluationError,
    evaluate_universe,
    evaluate_universe_inprocess,
)

PARITY_SCRIPTS: tuple[str, ...] = (
    """
import math


def screen(ctx):
    facts = ctx["facts"]
    bars = ctx["bars"]
    if not bars or facts["security_type"] != "common_stock":
        return False
    sma = ctx["indicators"].get("sma_5")
    if sma is None or sma[-1] is None:
        return False
    premium = (bars[-1]["close"] - sma[-1]) / sma[-1]
    return premium > 0.02 and math.isfinite(premium)
""",
    """
import statistics


def screen(ctx):
    closes = [bar["close"] for bar in ctx["bars"]]
    if len(closes) < 5:
        return False
    return statistics.pstdev(closes) > 1.0
""",
    """
def screen(ctx):
    facts = ctx["facts"]
    return {facts["symbol"]: facts["exchange"] == "NASDAQ"}
""",
    """
def screen(ctx):
    total = 0
    for bar in ctx["bars"]:
        total += bar["volume"] % 7
    return total > 3
""",
    """
def screen(ctx):
    return abs(0.1 + 0.2 - 0.3) < 1e-12
""",
    """
def screen(ctx):
    sma = ctx["indicators"].get("sma_5") or []
    warm = [value for value in sma if value is not None]
    if not warm:
        return False
    return max(warm) > min(warm)
""",
)

ALIASED_UNIVERSE: tuple[str, ...] = ("AAPL", "MSFT", "ETFA", "ZZTOP")


def _universe(make_context):
    return (
        make_context(symbol="AAPL"),
        make_context(symbol="MSFT", closes=("415",) * 10),
        make_context(
            symbol="ETFA",
            security_type="etf",
            closes=("88", "87", "89", "90", "91", "93", "94", "96", "98", "101"),
        ),
        make_context(symbol="ZZTOP", closes=("12", "13", "12", "14")),
    )


@pytest.mark.parametrize("script", PARITY_SCRIPTS)
def test_sandbox_equals_inprocess_reference(script: str, make_context) -> None:
    universe = _universe(make_context)
    sandbox = evaluate_universe(script, universe)
    reference = evaluate_universe_inprocess(script, universe)
    assert sandbox.decisions == reference.decisions
    assert sandbox.script_hash == reference.script_hash


def test_parity_holds_for_a_single_symbol(make_context) -> None:
    from harness.screening import evaluate_filter, evaluate_filter_inprocess

    context = make_context(symbol="AAPL")
    sandbox = evaluate_filter(PARITY_SCRIPTS[1], context)
    reference = evaluate_filter_inprocess(PARITY_SCRIPTS[1], context)
    assert sandbox.passed == reference.decisions["AAPL"]
    assert sandbox.script_hash == reference.script_hash


def test_parity_holds_with_empty_bar_window(make_context) -> None:
    script = """
def screen(ctx):
    return len(ctx["bars"]) == 0
"""
    universe = (make_context(symbol="EMPTY", closes=()), make_context("AAPL"))
    sandbox = evaluate_universe(script, universe)
    reference = evaluate_universe_inprocess(script, universe)
    assert sandbox.decisions == {"EMPTY": True, "AAPL": False}
    assert reference.decisions == sandbox.decisions


def test_parity_holds_for_missing_indicator_series(make_context) -> None:
    script = """
def screen(ctx):
    series = ctx["indicators"].get("rsi_14")
    if series is None or series[-1] is None:
        return False
    return series[-1] < 30
"""
    universe = _universe(make_context)
    sandbox = evaluate_universe(script, universe)
    reference = evaluate_universe_inprocess(script, universe)
    assert sandbox.decisions == {symbol: False for symbol in ALIASED_UNIVERSE}
    assert reference.decisions == sandbox.decisions


def test_violations_fail_closed_identically_in_both_models(make_context) -> None:
    script = "import os\n\ndef screen(ctx):\n    return True\n"
    universe = _universe(make_context)
    with pytest.raises(FilterEvaluationError) as sandbox_error:
        evaluate_universe(script, universe)
    with pytest.raises(FilterEvaluationError) as reference_error:
        evaluate_universe_inprocess(script, universe)
    sandbox_record = sandbox_error.value.error
    reference_record = reference_error.value.error
    assert sandbox_record.kind == reference_record.kind
    assert sandbox_record.message == reference_record.message
    assert sandbox_record.script_hash == reference_record.script_hash


def test_sandbox_is_deterministic_across_runs(make_context) -> None:
    universe = _universe(make_context)
    first = evaluate_universe(PARITY_SCRIPTS[0], universe)
    second = evaluate_universe(PARITY_SCRIPTS[0], universe)
    assert first.decisions == second.decisions
    assert first.script_hash == second.script_hash


def test_reference_and_sandbox_share_the_script_contract(
    make_context, golden_script
) -> None:
    universe = _universe(make_context)
    sandbox = evaluate_universe(golden_script, universe)
    reference = evaluate_universe_inprocess(golden_script, universe)
    assert sandbox.decisions == {
        "AAPL": True,
        "MSFT": False,
        "ETFA": False,
        "ZZTOP": False,
    }
    assert reference.decisions == sandbox.decisions


def test_float_edge_values_survive_the_wire(make_context) -> None:
    from harness.screening import evaluate_filter, evaluate_filter_inprocess

    script = """
def screen(ctx):
    close = ctx["bars"][0]["close"]
    return close == close and 1.0 / 3.0 * 3.0 == 1.0
"""
    context: FilterContext = make_context(symbol="AAPL")
    assert evaluate_filter(script, context).passed is True
    assert evaluate_filter_inprocess(script, context).decisions["AAPL"] is True
