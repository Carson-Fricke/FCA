"""The execution-store rebuild fold (REQ-B4).

A pure, I/O-free fold from an ordered transition-with-completion sequence
(:class:`~thesis.workflows.core.execution_store.StoreTransition`) into the
workflow's typed :class:`HypothesisDevelopmentState`.  Each transition moves
the phase and carries its completion contract; the fold dispatches on
``(from_phase, to_phase)`` to apply the contract's payload to the typed state:

- the creation→trials contract's ``hypothesis`` payload becomes the draft,
- the trials→verdict contract's ``trials`` list becomes ``state.trials``,
- verdict contracts set ``state.verdict``,
- the verdict→creation circle-back resets to a fresh creation state per
  ``reset_premise`` semantics.

The hypothesis document side-store is deliberately not read: the fold is
store-only, so a rebuild never reaches into the workspace.  Strategy-chain
payload handlers are additive follow-ups in the dispatch table.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from thesis.workflows.core.execution_store import StoreTransition

from .contracts.trials import FalsificationTrial
from .contracts.verdict import FalsificationVerdict
from .errors import WorkflowError
from .phases import (
    _FALSIFICATION_TRIALS,
    _HYPOTHESIS_CREATION,
    _STRATEGY_DESIGN,
    _STRATEGY_RCA,
    _VERDICT,
    HypothesisDevelopmentPhase,
    PhaseTransition,
)
from .phases.trials import _draft_from_contract
from .piping import reset_backtests, reset_premise
from .state import HypothesisDevelopmentState

#: One transition's payload fold: typed state in, typed state out.
PayloadFold = Callable[
    [HypothesisDevelopmentState, Mapping[str, object]],
    HypothesisDevelopmentState,
]


def _phase(value: str) -> HypothesisDevelopmentPhase:
    try:
        return HypothesisDevelopmentPhase(value)
    except ValueError as error:
        raise WorkflowError(
            f"stored transition names an unknown phase: {value!r}"
        ) from error


def _required_payload(
    completion: Mapping[str, object], key: str, transition: tuple[str, str]
) -> Mapping[str, object]:
    payload = completion.get(key)
    if not isinstance(payload, Mapping) or not payload:
        raise WorkflowError(
            f"the {transition[0]}→{transition[1]} completion requires {key!r}"
        )
    return payload


def _verdict_from_contract(payload: Mapping[str, object]) -> FalsificationVerdict:
    raw = payload.get("trials")
    trials = (
        tuple(FalsificationTrial.from_dict(item) for item in raw)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
        else ()
    )
    return FalsificationVerdict(
        outcome=str(payload.get("outcome", "")),
        reason=str(payload.get("reason", "")),
        trials=trials,
    )


def _fold_draft(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the accepted hypothesis contract as the run's draft."""
    payload = _required_payload(
        completion,
        "hypothesis",
        (_HYPOTHESIS_CREATION.value, _FALSIFICATION_TRIALS.value),
    )
    return replace(state, draft=_draft_from_contract(payload))


def _fold_trials(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the trials phase's recorded trials, round-tripped from the store."""
    raw = completion.get("trials")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise WorkflowError("the trials→verdict completion requires a trials list")
    trials = tuple(FalsificationTrial.from_dict(item) for item in raw)
    return replace(state, trials=trials)


def _fold_verdict(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the verdict contract as the run's verdict."""
    payload = _required_payload(
        completion,
        "verdict",
        (_VERDICT.value, _STRATEGY_DESIGN.value),
    )
    return replace(state, verdict=_verdict_from_contract(payload))


def _fold_circle_back(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Record the refuting verdict, then reset to a fresh creation state."""
    payload = completion.get("verdict")
    if isinstance(payload, Mapping) and payload:
        state = replace(state, verdict=_verdict_from_contract(payload))
    return reset_premise(state)


def _fold_reset_backtests(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Clear the previous revision's backtests before a redesign."""
    del completion
    return reset_backtests(state)


#: The per-transition payload folds.  Unlisted transitions (the strategy
#: chain's structural moves) carry state unchanged; handlers are additive
#: follow-ups keyed by their (from_phase, to_phase) pair.
_PAYLOAD_FOLDS: Mapping[
    tuple[HypothesisDevelopmentPhase, HypothesisDevelopmentPhase],
    PayloadFold,
] = {
    (_HYPOTHESIS_CREATION, _FALSIFICATION_TRIALS): _fold_draft,
    (_FALSIFICATION_TRIALS, _VERDICT): _fold_trials,
    (_VERDICT, _STRATEGY_DESIGN): _fold_verdict,
    (_VERDICT, _HYPOTHESIS_CREATION): _fold_circle_back,
    (_STRATEGY_RCA, _STRATEGY_DESIGN): _fold_reset_backtests,
}


def rebuild_state(
    workspace_id: str,
    execution_id: str,
    transitions: Sequence[StoreTransition],
    *,
    initial_phase: HypothesisDevelopmentPhase = HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
) -> HypothesisDevelopmentState:
    """Fold an ordered transition sequence into the typed run state.

    The fold starts from the workflow's fresh state and applies each stored
    transition in order; the resulting state's phase, epoch, and transition
    records mirror the store exactly.  ``initial_phase`` is the phase a run
    rests in before its first transition: live runs start at the workflow's
    declared initial phase.
    """
    state = HypothesisDevelopmentState(
        workspace_id=workspace_id,
        execution_id=execution_id,
        phase=initial_phase,
        intent_epoch=0,
    )
    for transition in transitions:
        state = fold_transition(state, transition)
    return state


def fold_transition(
    state: HypothesisDevelopmentState,
    transition: StoreTransition,
) -> HypothesisDevelopmentState:
    """Fold one stored transition into the typed state."""
    source = _phase(transition.from_phase)
    target = _phase(transition.to_phase)
    state = replace(
        state,
        phase=target,
        intent_epoch=transition.intent_epoch,
        transitions=(
            *state.transitions,
            PhaseTransition(
                sequence=transition.sequence,
                from_phase=source,
                to_phase=target,
                at=transition.occurred_at,
                intent_epoch=transition.intent_epoch,
                reason=transition.reason,
            ),
        ),
    )
    handler = _PAYLOAD_FOLDS.get((source, target))
    if handler is not None:
        state = handler(state, transition.completion)
    return state


__all__ = [
    "fold_transition",
    "rebuild_state",
]
