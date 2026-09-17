from __future__ import annotations

import pytest

from harness.research import (
    ResearchProgramError,
    evaluate_indicator,
    fit_model,
    observe_model,
)
from harness.research.adapter import ResearchModelMonitor


def test_model_returns_arbitrary_finite_named_metrics() -> None:
    source = """
import numpy as np

def fit(context):
    return {"scale": 1.0}

def observe(fit, context):
    left = np.asarray(context["left"])
    right = np.asarray(context["right"])
    return {
        "rolling_corr_60d_median": np.corrcoef(left, right)[0, 1],
        "sample_count": len(left),
    }
"""

    fit = fit_model(
        source,
        "fit",
        {"left": [1.0, 2.0, 3.0], "right": [2.0, 4.0, 6.0]},
    )
    metrics = observe_model(
        source,
        "observe",
        fit,
        {"left": [1.0, 2.0, 3.0], "right": [2.0, 4.0, 6.0]},
    )

    assert metrics == {"rolling_corr_60d_median": 1.0, "sample_count": 3.0}


def test_workspace_indicator_returns_a_finite_numeric_tree() -> None:
    source = """
def calculate(context, parameters):
    window = parameters["window"]
    return context["values"][-window:]
"""

    result = evaluate_indicator(
        source,
        "calculate",
        {"values": [1.0, 2.0, 3.0]},
        {"window": 2},
    )

    assert result == [2.0, 3.0]


def test_observe_uses_frozen_fit_without_calling_fit() -> None:
    source = """
def fit(context):
    raise AssertionError("fit must not run in Layer B")

def observe(fit, context):
    return {"model_health": fit["baseline"]}
"""
    monitor = ResearchModelMonitor(
        model_source=source,
        fit={"baseline": 0.73},
    )

    assert monitor.observe() == {"model_health": 0.73}


@pytest.mark.parametrize(
    "source, message",
    [
        (
            "import os\ndef fit(context):\n    return {'x': 1}\n\ndef observe(fit, context):\n    return {'x': 1}\n",
            "imports",
        ),
        (
            "def fit(context):\n    return {'x': float('inf')}\n\ndef observe(fit, context):\n    return {'x': 1}\n",
            "finite",
        ),
        (
            "def fit(context):\n    return {'x': True}\n\ndef observe(fit, context):\n    return {'x': 1}\n",
            "numeric",
        ),
    ],
)
def test_model_source_and_outputs_fail_closed(source: str, message: str) -> None:
    with pytest.raises(ResearchProgramError, match=message):
        fit_model(source, "fit", {})
