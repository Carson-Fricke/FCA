"""Workflow error contracts.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations


class WorkflowError(RuntimeError):
    """Base error for invalid hypothesis-development workflow operations."""


class HypothesisDevelopmentIntakeError(WorkflowError):
    """Raised when a hypothesis-development request is not valid intake."""


class FalsificationComposeError(WorkflowError):
    """Raised when model output cannot become falsification content."""
