"""Request, hypothesis, plan, trial, verdict, strategy, and RCA contracts.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.  Each subject module owns its own
contracts and this package re-exports the complete public surface.
"""

from __future__ import annotations

from .creation import HypothesisDevelopmentRequest
from .hypothesis import HypothesisDraft
from .plan import (
    FalsificationPlan,
    FalsificationPlanDraft,
    FalsifierTest,
    PlaceboControl,
    PlannedTest,
)
from .rca import RcaDiagnosis
from .strategy import ExitCriterion, StrategyContent, StrategyDesignDraft
from .trials import FalsificationTrial
from .verdict import FalsificationVerdict

__all__ = [
    "ExitCriterion",
    "FalsificationPlan",
    "FalsificationPlanDraft",
    "FalsificationTrial",
    "FalsificationVerdict",
    "FalsifierTest",
    "HypothesisDevelopmentRequest",
    "HypothesisDraft",
    "PlaceboControl",
    "PlannedTest",
    "RcaDiagnosis",
    "StrategyContent",
    "StrategyDesignDraft",
]
