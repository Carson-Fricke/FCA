"""Focused regression report comparing fresh benchmarks with baselines.

The report flags every metric that regressed beyond the 20 percent budget
recorded in ``baselines.json``.  The test only runs when ``FCA_PERF=1``
(see ``conftest.py``) and is the CI-facing regression gate described in
``.github/performance.md``.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.performance

BASELINE_PATH: Final = Path(__file__).resolve().parent / "baselines.json"
UPDATE_BASELINES_ENV: Final = "FCA_PERF_UPDATE_BASELINES"
REPORT_ONLY_ENV: Final = "FCA_PERF_REPORT_ONLY"


def _report_lines(document: dict, fresh: dict, budget: float) -> list[str]:
    lines: list[str] = []
    for name, entry in document["metrics"].items():
        measured = fresh.get(name)
        if measured is None:
            lines.append(f"[missing] {name}: benchmark produced no fresh value")
            continue
        baseline = float(entry["value"])
        unit = str(entry["unit"])
        higher_is_better = bool(entry["higher_is_better"])
        if baseline <= 0:
            lines.append(f"[skipped] {name}: non-positive baseline {baseline}")
            continue
        delta = (measured.value - baseline) / baseline
        regressed = (higher_is_better and delta < -budget) or (
            not higher_is_better and delta > budget
        )
        marker = "REGRESSION" if regressed else "ok"
        lines.append(
            f"[{marker}] {name}: baseline {baseline:,.2f} {unit}, "
            f"fresh {measured.value:,.2f} {unit}, delta {delta:+.1%} "
            f"(budget {budget:.0%})"
        )
    for name in sorted(set(fresh) - set(document["metrics"])):
        lines.append(f"[new] {name}: no baseline entry")
    return lines


def test_benchmark_regression_report(perf) -> None:
    if os.environ.get(UPDATE_BASELINES_ENV) == "1":
        perf.write_baselines(BASELINE_PATH)
        return

    if not BASELINE_PATH.exists():
        pytest.fail(
            "no captured baseline document at "
            f"{BASELINE_PATH}; run the suite once with "
            "FCA_PERF=1 and FCA_PERF_UPDATE_BASELINES=1 to capture it"
        )
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    fresh = dict(perf.run_all())
    budget = float(document["regression_budget_percent"]) / 100
    lines = _report_lines(document, fresh, budget)
    report = "\n".join(
        (
            "performance regression report",
            f"baseline document: {BASELINE_PATH}",
            f"regression budget: {budget:.0%} in the unfavorable direction",
            *lines,
        )
    )
    if os.environ.get(REPORT_ONLY_ENV) == "1":
        if any(line.startswith("[REGRESSION]") for line in lines):
            warnings.warn(report, UserWarning, stacklevel=2)
        return
    assert not any(line.startswith("[REGRESSION]") for line in lines), report
