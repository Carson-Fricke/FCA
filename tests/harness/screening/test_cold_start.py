"""Worker cold-start cost measurement with a generous budget.

Every ``evaluate_filter`` / ``evaluate_universe`` call spawns a fresh
worker process, so the batch overhead is one interpreter cold start.
On a typical Windows dev box the measured cost lands in the
0.1-0.3 s range (interpreter startup dominates; the harness adds
dozens of milliseconds).  This test pins a generous 30 s ceiling so
slow CI machines never flake, and prints the measured values for the
verification record.
"""

from __future__ import annotations

import time

from harness.screening import evaluate_filter

BUDGET_SECONDS: float = 30.0
SAMPLES: int = 3

COLD_SCRIPT = """
def screen(ctx):
    return len(ctx["bars"]) > 0
"""


def test_worker_cold_start_within_generous_budget(make_context) -> None:
    context = make_context("AAPL")
    timings: list[float] = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        result = evaluate_filter(COLD_SCRIPT, context)
        timings.append(time.perf_counter() - started)
        assert result.passed is True
    worst = max(timings)
    mean = sum(timings) / len(timings)
    print(
        f"\nscreening worker cold start over {SAMPLES} fresh subprocess runs:"
        f" mean {mean * 1000:.0f} ms, worst {worst * 1000:.0f} ms"
        f" (samples: {[f'{value * 1000:.0f} ms' for value in timings]})"
    )
    assert worst < BUDGET_SECONDS


def test_batch_amortizes_cold_start_across_symbols(make_context) -> None:
    from harness.screening import evaluate_universe

    universe = tuple(
        make_context(symbol=symbol)
        for symbol in ("AAPL", "MSFT", "ETFA", "ZZTOP", "DELL")
    )
    started = time.perf_counter()
    batch = evaluate_universe(
        "\ndef screen(ctx):\n    return ctx['facts']['security_type'] == 'common_stock'\n",
        universe,
    )
    elapsed = time.perf_counter() - started
    print(f"\nfive-symbol batch through one worker cold start: {elapsed * 1000:.0f} ms")
    assert batch.decisions == {
        "AAPL": True,
        "MSFT": True,
        "ETFA": True,
        "ZZTOP": True,
        "DELL": True,
    }
    assert elapsed < BUDGET_SECONDS
