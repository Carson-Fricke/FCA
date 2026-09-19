"""Prompt builders and composer protocols.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.  The package re-exports the full prompt
surface so callers can keep importing from ``prompts``.
"""

from __future__ import annotations

from .creation import (
    HypothesisComposer,
    _creation_finalize_prompt,
    _creation_turn_prompt,
    _event_output_text,
)
from .falsification import FalsificationPlanner, _falsification_turn_prompt
from .rca import RcaComposer
from .strategy import StrategyDesigner, _hypothesis_context_lines

__all__ = [
    "FalsificationPlanner",
    "HypothesisComposer",
    "RcaComposer",
    "StrategyDesigner",
    "_creation_finalize_prompt",
    "_creation_turn_prompt",
    "_event_output_text",
    "_falsification_turn_prompt",
    "_hypothesis_context_lines",
]
