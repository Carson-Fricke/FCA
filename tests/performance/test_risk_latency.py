"""Risk-pipeline latency benchmarks per decision."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.performance

PATHS = ("allow", "reduce")


@pytest.mark.parametrize("path", PATHS)
def test_risk_latency_baseline(perf, path) -> None:
    metric = perf.benchmark_risk_latency(path)

    assert metric.value > 0
