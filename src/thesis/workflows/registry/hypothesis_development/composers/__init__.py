"""JSON composers that turn model output into contracts.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.  The package re-exports the composer
surface so callers can keep importing from ``composers``.
"""

from __future__ import annotations

from ._shared import _json_document
from .hypothesis import JsonDevelopmentHypothesisComposer
from .plan import JsonFalsificationPlanner
from .rca import JsonRcaComposer
from .strategy import JsonStrategyDesigner

__all__ = [
    "JsonDevelopmentHypothesisComposer",
    "JsonFalsificationPlanner",
    "JsonRcaComposer",
    "JsonStrategyDesigner",
    "_json_document",
]
