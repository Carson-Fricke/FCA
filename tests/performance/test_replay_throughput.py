"""Replay throughput benchmarks across universe-width tiers."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.performance

TIERS = ("narrow", "medium", "wide")


@pytest.mark.parametrize("tier", TIERS)
def test_replay_throughput_baseline(perf, tier) -> None:
    metric = perf.benchmark_replay_throughput(tier)

    assert metric.value > 0
