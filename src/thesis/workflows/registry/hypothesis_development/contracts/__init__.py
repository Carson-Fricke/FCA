"""Request, hypothesis, plan, trial, verdict, and RCA contracts.

Each subject module owns its own contracts and this package re-exports the
public surface.
"""

from __future__ import annotations

from .creation import HypothesisDevelopmentRequest
from .hypothesis import HypothesisDraft
from .plan import FalsificationPlan, FalsifierTest, PlaceboControl
from .rca import RcaDiagnosis
from .strategy import ExitCriterion
from .trials import FalsificationTrial
from .verdict import FalsificationVerdict

__all__ = [
    "ExitCriterion",
    "FalsificationPlan",
    "FalsificationTrial",
    "FalsificationVerdict",
    "FalsifierTest",
    "HypothesisDevelopmentRequest",
    "HypothesisDraft",
    "PlaceboControl",
    "RcaDiagnosis",
]
