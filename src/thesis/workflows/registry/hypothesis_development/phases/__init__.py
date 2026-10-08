"""Phase vocabulary, transition table, and phase metadata.

The live workflow is discovery -> model creation -> falsification trials ->
verdict -> the strategy chain.  ``HYPOTHESIS_CREATION`` is a retired member
kept only so stored runs written before the model-creation rename stay
readable: :func:`phase_from_value` decodes it to ``MODEL_CREATION``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from thesis.chat.contracts import WorkflowDefinition

from ..errors import InvalidPhaseTransition, WorkflowError
from ..primitives import _WORKFLOW_ID, _WORKFLOW_TITLE, _WORKFLOW_VERSION, _iso

__all__ = ["phase_from_value"]


class HypothesisDevelopmentPhase(StrEnum):
    """The explicit, ordered phases of the hypothesis-development workflow."""

    #: Retired: the workflow now starts in DISCOVERY and models in
    #: MODEL_CREATION.  Stored runs that name this value decode to
    #: MODEL_CREATION through :func:`phase_from_value`.
    HYPOTHESIS_CREATION = "hypothesis_creation"
    DISCOVERY = "discovery"
    MODEL_CREATION = "model_creation"
    FALSIFICATION_TRIALS = "falsification_trials"
    VERDICT = "verdict"
    STRATEGY_DESIGN = "strategy_design"
    STRATEGY_BACKTESTING = "strategy_backtesting"
    STRATEGY_RCA = "strategy_rca"
    OPTIMIZATION = "optimization"
    NULL_TESTING = "null_testing"
    VARIATION = "variation"
    FORWARD_VALIDATION = "forward_validation"
    COMPLETED = "completed"
    FAILED = "failed"


_RETIRED_PHASES: Mapping[str, HypothesisDevelopmentPhase] = {
    HypothesisDevelopmentPhase.HYPOTHESIS_CREATION.value: (
        HypothesisDevelopmentPhase.MODEL_CREATION
    ),
}


def phase_from_value(value: str) -> HypothesisDevelopmentPhase:
    """Decode one stored phase value, mapping retired names forward.

    A run stored before the model-creation rename names
    ``hypothesis_creation``; it decodes to ``model_creation`` so the old run
    remains readable and resumes in the live vocabulary.
    """
    retired = _RETIRED_PHASES.get(value)
    if retired is not None:
        return retired
    try:
        return HypothesisDevelopmentPhase(value)
    except ValueError as error:
        raise WorkflowError(
            f"stored transition names an unknown phase: {value!r}"
        ) from error


_DISCOVERY = HypothesisDevelopmentPhase.DISCOVERY


_MODEL_CREATION = HypothesisDevelopmentPhase.MODEL_CREATION


_FALSIFICATION_TRIALS = HypothesisDevelopmentPhase.FALSIFICATION_TRIALS


_VERDICT = HypothesisDevelopmentPhase.VERDICT


_STRATEGY_DESIGN = HypothesisDevelopmentPhase.STRATEGY_DESIGN


_STRATEGY_BACKTESTING = HypothesisDevelopmentPhase.STRATEGY_BACKTESTING


_STRATEGY_RCA = HypothesisDevelopmentPhase.STRATEGY_RCA


_OPTIMIZATION = HypothesisDevelopmentPhase.OPTIMIZATION


_NULL_TESTING = HypothesisDevelopmentPhase.NULL_TESTING


_VARIATION = HypothesisDevelopmentPhase.VARIATION


_FORWARD_VALIDATION = HypothesisDevelopmentPhase.FORWARD_VALIDATION


_COMPLETED = HypothesisDevelopmentPhase.COMPLETED


_FAILED = HypothesisDevelopmentPhase.FAILED


def workflow_definition() -> WorkflowDefinition:
    from ..spec import phase_declarations

    return WorkflowDefinition(
        workflow_id=_WORKFLOW_ID,
        version=_WORKFLOW_VERSION,
        title=_WORKFLOW_TITLE,
        phases=tuple(
            item.spec.phase for item in phase_declarations() if not item.spec.terminal
        ),
    )


def transition_phase(
    current: HypothesisDevelopmentPhase, target: HypothesisDevelopmentPhase
) -> HypothesisDevelopmentPhase:
    """Move between phases through the explicit transition table."""
    from ..spec import phase_declarations

    declaration = next(
        item for item in phase_declarations() if item.spec.phase == current.value
    )
    if target.value not in declaration.edges:
        raise InvalidPhaseTransition(
            f"workflow phase cannot move from {current.value} to {target.value}"
        )
    return target


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    """One immutable, ordered record of a workflow phase change."""

    sequence: int
    from_phase: HypothesisDevelopmentPhase
    to_phase: HypothesisDevelopmentPhase
    at: datetime
    intent_epoch: int
    reason: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise WorkflowError("phase transition sequence starts at one")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise WorkflowError("phase transition timestamp must be timezone-aware")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise WorkflowError("phase transition reason must be non-empty")
        object.__setattr__(self, "at", self.at.astimezone(UTC))

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this transition."""
        return {
            "at": _iso(self.at),
            "fromPhase": self.from_phase.value,
            "intentEpoch": self.intent_epoch,
            "reason": self.reason,
            "sequence": self.sequence,
            "toPhase": self.to_phase.value,
        }
