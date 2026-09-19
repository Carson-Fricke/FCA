"""Deterministic acceptance gates for each phase.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.  The package re-exports the full gate
surface so callers can keep importing from ``gates``.
"""

from __future__ import annotations

from .evidence import check_evidence_bound
from .falsifiability import check_falsifiability
from .plan import check_falsification_plan
from .research_model import check_research_model
from .strategy import check_strategy_design, check_verdict_advances

__all__ = [
    "check_evidence_bound",
    "check_falsifiability",
    "check_falsification_plan",
    "check_research_model",
    "check_strategy_design",
    "check_verdict_advances",
]
