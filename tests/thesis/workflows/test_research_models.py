"""Focused coverage for research-model fit-parameter extraction."""

from __future__ import annotations

import pytest

from thesis.workflows.research_models import _extract_fit


def test_fit_metrics_are_extracted_from_the_custom_block() -> None:
    fit = _extract_fit({"custom": {"fit.baseline": "0.5", "sharpe": 1.0}})
    assert fit == {"baseline": 0.5}


def test_missing_fit_metrics_name_the_published_metrics() -> None:
    with pytest.raises(ValueError) as error:
        _extract_fit({"custom": {"data_ok": "1", "sharpe": "1.0"}})

    message = str(error.value)
    assert "published no fit parameters" in message
    assert "data_ok" in message
    assert "sharpe" in message
