"""Streaming memory budget benchmarks across universe-width tiers."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.performance

TIERS = ("narrow", "medium", "wide")


@pytest.mark.parametrize("tier", TIERS)
def test_streaming_memory_baseline(perf, tier) -> None:
    peak, per_event = perf.benchmark_memory_profile(tier)

    assert peak.value > 0
    assert per_event.value > 0
