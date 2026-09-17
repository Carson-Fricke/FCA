"""Execution contract for statistical research models and custom indicators.

Research code receives plain, JSON-shaped historical data. It never receives a
strategy context, order capability, portfolio, or venue. The platform-owned
adapter is the only component that bridges model outputs into a backtest run's
custom metric channel.
"""

from harness.research.adapter import ResearchModelAdapter, ResearchModelMonitor
from harness.research.programs import (
    ResearchProgramError,
    evaluate_indicator,
    fit_model,
    observe_model,
)

__all__ = [
    "ResearchModelAdapter",
    "ResearchModelMonitor",
    "ResearchProgramError",
    "evaluate_indicator",
    "fit_model",
    "observe_model",
]
