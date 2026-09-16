"""Workflow error, control-flow, and conversational type contracts.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import HypothesisDevelopmentState


class WorkflowError(RuntimeError):
    """Base error for invalid hypothesis-development workflow operations."""


class InvalidPhaseTransition(WorkflowError):
    """A phase cannot move directly to the requested phase."""


class HypothesisDevelopmentIntakeError(WorkflowError):
    """Raised when a hypothesis-development request is not valid intake."""


class FalsificationComposeError(WorkflowError):
    """Raised when model output cannot become falsification content."""


@dataclass(frozen=True, slots=True)
class CreationTurn:
    """Structured conversational judgment for the hypothesis-creation phase."""

    message: str
    advance: bool

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message.strip():
            raise FalsificationComposeError("creation turn message must be non-empty")
        if not isinstance(self.advance, bool):
            raise FalsificationComposeError("creation turn advance must be boolean")
        object.__setattr__(self, "message", self.message.strip())


class _Fenced(Exception):
    """Internal signal that a gate or steering event must pause the workflow."""

    def __init__(
        self,
        message: str,
        state: HypothesisDevelopmentState | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state


class _PhaseFailure(WorkflowError):
    """Internal failure signal that carries the partial workflow state."""

    def __init__(self, message: str, state: HypothesisDevelopmentState) -> None:
        super().__init__(message)
        self.state = state
