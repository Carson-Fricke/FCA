"""Fill-model decision cost benchmarks per order."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.performance

MODELS = ("simple_touch", "trade_through")


@pytest.mark.parametrize("model", MODELS)
def test_matching_cost_baseline(perf, model) -> None:
    metric = perf.benchmark_matching_cost(model)

    assert metric.value > 0
