"""Phase vocabulary, transition table, and phase metadata.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from thesis.chat.contracts import WorkflowDefinition
from thesis.workflows.core.workflow import ActiveSubAgent as _ActiveSubAgent

from ..errors import InvalidPhaseTransition, WorkflowError
from ..primitives import _WORKFLOW_ID, _WORKFLOW_TITLE, _WORKFLOW_VERSION, _iso

__all__ = ["_ActiveSubAgent"]


class HypothesisDevelopmentPhase(StrEnum):
    """The explicit, ordered phases of the hypothesis-development workflow."""

    HYPOTHESIS_CREATION = "hypothesis_creation"
    FALSIFICATION_TRIALS = "falsification_trials"
    VERDICT = "verdict"
    STRATEGY_DESIGN = "strategy_design"
    STRATEGY_BACKTESTING = "strategy_backtesting"
    STRATEGY_RCA = "strategy_rca"
    OPTIMIZATION = "optimization"
    COMPLETED = "completed"
    FAILED = "failed"


_HYPOTHESIS_CREATION = HypothesisDevelopmentPhase.HYPOTHESIS_CREATION


_FALSIFICATION_TRIALS = HypothesisDevelopmentPhase.FALSIFICATION_TRIALS


_VERDICT = HypothesisDevelopmentPhase.VERDICT


_STRATEGY_DESIGN = HypothesisDevelopmentPhase.STRATEGY_DESIGN


_STRATEGY_BACKTESTING = HypothesisDevelopmentPhase.STRATEGY_BACKTESTING


_STRATEGY_RCA = HypothesisDevelopmentPhase.STRATEGY_RCA


_OPTIMIZATION = HypothesisDevelopmentPhase.OPTIMIZATION


_COMPLETED = HypothesisDevelopmentPhase.COMPLETED


_FAILED = HypothesisDevelopmentPhase.FAILED


_ACTIVE_PHASES = (
    _HYPOTHESIS_CREATION,
    _VERDICT,
    _STRATEGY_DESIGN,
    _STRATEGY_BACKTESTING,
    _STRATEGY_RCA,
    _OPTIMIZATION,
)


#: Role family and human label for each phase's top-level orchestrator.
#: The deterministic falsification-trials phase is not a sub-agent (REQ-C1):
#: it has no durable node and no orchestrator, so it is not listed here.
_PHASE_ORCHESTRATORS: Mapping[HypothesisDevelopmentPhase, tuple[str, str]] = {
    _HYPOTHESIS_CREATION: (
        "research",
        "Thesis hypothesis-creation orchestrator",
    ),
    _VERDICT: ("research", "Thesis research verdict orchestrator"),
    _STRATEGY_DESIGN: ("coding", "Thesis coding strategy-design orchestrator"),
    _STRATEGY_BACKTESTING: (
        "coding",
        "Thesis strategy-backtesting orchestrator",
    ),
    _STRATEGY_RCA: ("critique", "Thesis strategy root-cause orchestrator"),
    _OPTIMIZATION: ("coding", "Thesis coding optimization orchestrator"),
}


#: What each phase orchestrator is prompted to do, recorded as visible thinking
#: so the operator can see the phase's intent even when it makes no model call.
#: The deterministic falsification-trials phase is not a sub-agent (REQ-C1):
#: it has no durable node and no orchestrator, so it declares no task here.
_PHASE_TASKS: Mapping[HypothesisDevelopmentPhase, str] = {
    _HYPOTHESIS_CREATION: (
        "Turn the operator's intuition into one statistical model of the assets "
        "involved, estimated on historical data: the regularity it predicts, the "
        "no-structure null it must beat, the Layer A falsifiers that would refute "
        "it, and the negative controls."
    ),
    _VERDICT: "Record the falsification verdict from the trial results.",
    _STRATEGY_DESIGN: (
        "Implement a sandbox-valid strategy whose exits re-check the "
        "hypothesis falsifiers."
    ),
    _STRATEGY_BACKTESTING: (
        "Run the designed strategy through the harness and record its test result."
    ),
    _STRATEGY_RCA: (
        "Diagnose the failed strategy backtest and prescribe a concrete revision."
    ),
    _OPTIMIZATION: "Run the optimization backtest for the designed strategy.",
}


_TRANSITIONS: Mapping[
    HypothesisDevelopmentPhase, frozenset[HypothesisDevelopmentPhase]
] = {
    _HYPOTHESIS_CREATION: frozenset({_FALSIFICATION_TRIALS, _FAILED}),
    _FALSIFICATION_TRIALS: frozenset({_VERDICT, _FAILED}),
    _VERDICT: frozenset({_STRATEGY_DESIGN, _HYPOTHESIS_CREATION, _FAILED}),
    _STRATEGY_DESIGN: frozenset({_STRATEGY_BACKTESTING, _FAILED}),
    _STRATEGY_BACKTESTING: frozenset({_OPTIMIZATION, _STRATEGY_RCA, _FAILED}),
    _STRATEGY_RCA: frozenset({_STRATEGY_DESIGN, _FAILED}),
    _OPTIMIZATION: frozenset({_COMPLETED, _STRATEGY_RCA, _FAILED}),
    _COMPLETED: frozenset(),
    _FAILED: frozenset(),
}


HYPOTHESIS_DEVELOPMENT_PHASES: tuple[str, ...] = (
    _HYPOTHESIS_CREATION.value,
    _FALSIFICATION_TRIALS.value,
    _VERDICT.value,
    _STRATEGY_DESIGN.value,
    _STRATEGY_BACKTESTING.value,
    _STRATEGY_RCA.value,
    _OPTIMIZATION.value,
)


HYPOTHESIS_DEVELOPMENT_WORKFLOW = WorkflowDefinition(
    workflow_id=_WORKFLOW_ID,
    version=_WORKFLOW_VERSION,
    title=_WORKFLOW_TITLE,
    phases=HYPOTHESIS_DEVELOPMENT_PHASES,
)


def transition_phase(
    current: HypothesisDevelopmentPhase, target: HypothesisDevelopmentPhase
) -> HypothesisDevelopmentPhase:
    """Move between phases through the explicit transition table."""
    if target not in _TRANSITIONS[current]:
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
