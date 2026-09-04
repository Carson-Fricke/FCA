"""Recorder artifact write throughput benchmark."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.performance


def test_recorder_artifact_write_baseline(perf) -> None:
    metric = perf.benchmark_recorder_throughput()

    assert metric.value > 0
