"""Request, discovery, hypothesis, plan, trial, verdict, candidate, null,
variation, forward, and RCA contracts.

Each subject module owns its own contracts and this package re-exports the
public surface.
"""

from __future__ import annotations

from .candidates import (
    CandidateAssignment,
    CandidateEvaluation,
    CandidateShortlistEntry,
)
from .creation import HypothesisDevelopmentRequest
from .discovery import DiscoveryAttempt, DiscoveryCandidate, DiscoveryDraft
from .evidence import AssumptionEvidence, ControlEvidence, ResearchEvidence
from .forward import (
    ForwardEvaluation,
    ForwardNullEvidence,
    ForwardVariationEvidence,
    ForwardWindow,
)
from .hypothesis import HypothesisDraft
from .nulls import (
    NullTestEvaluation,
    NullTestObservation,
    NullTestReplicate,
)
from .plan import FalsificationPlan, FalsifierTest, PlaceboControl
from .rca import RcaDiagnosis
from .strategy import (
    STRATEGY_BACKTEST_STATES,
    StrategyBacktest,
    program_reference,
    strategy_backtest_payload_errors,
)
from .trials import FalsificationTrial
from .variation import (
    VariationCenter,
    VariationEvaluation,
    VariationPoint,
)
from .verdict import FalsificationVerdict

__all__ = [
    "AssumptionEvidence",
    "CandidateAssignment",
    "CandidateEvaluation",
    "CandidateShortlistEntry",
    "ControlEvidence",
    "DiscoveryAttempt",
    "DiscoveryCandidate",
    "DiscoveryDraft",
    "STRATEGY_BACKTEST_STATES",
    "StrategyBacktest",
    "program_reference",
    "strategy_backtest_payload_errors",
    "FalsificationPlan",
    "FalsificationTrial",
    "FalsificationVerdict",
    "FalsifierTest",
    "ForwardEvaluation",
    "ForwardNullEvidence",
    "ForwardVariationEvidence",
    "ForwardWindow",
    "HypothesisDevelopmentRequest",
    "HypothesisDraft",
    "NullTestEvaluation",
    "NullTestObservation",
    "NullTestReplicate",
    "PlaceboControl",
    "RcaDiagnosis",
    "ResearchEvidence",
    "VariationCenter",
    "VariationEvaluation",
    "VariationPoint",
]
