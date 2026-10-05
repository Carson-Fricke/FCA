"""The execution-store rebuild fold (REQ-B4).

A pure, I/O-free fold from an ordered transition-with-completion sequence
(:class:`~thesis.workflows.core.execution_store.StoreTransition`) into the
workflow's typed :class:`HypothesisDevelopmentState`.  Each transition moves
the phase and carries its completion contract; the fold dispatches on
``(from_phase, to_phase)`` to apply the contract's payload to the typed state:

- the discovery→model-creation contract's ``discovery`` payload becomes the
  discovery draft, and the model artifacts are reset while keeping it,
- the model-creation→trials contract's ``hypothesis`` payload becomes the draft,
- the trials→verdict contract's ``trials`` list becomes ``state.trials``,
- verdict contracts set ``state.verdict``,
- the verdict→model-creation circle-back records the verdict and resets to a
  fresh model state per ``reset_premise`` semantics (keeping discovery),
- the verdict→discovery restart records the verdict and resets everything per
  ``reset_discovery`` semantics.

Stored runs written before the model-creation rename name the retired
``hypothesis_creation`` value; :func:`phase_from_value` decodes it to
``model_creation`` so the old transitions fold through the live handlers.

The hypothesis document side-store is deliberately not read: the fold is
store-only, so a rebuild never reaches into the workspace.  Strategy-chain
payload handlers are additive follow-ups in the dispatch table.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from thesis.workflows.core.execution_store import StoreTransition

from .contracts.discovery import DiscoveryDraft
from .contracts.trials import FalsificationTrial
from .contracts.verdict import FalsificationVerdict
from .errors import WorkflowError
from .phases import (
    _DISCOVERY,
    _FALSIFICATION_TRIALS,
    _MODEL_CREATION,
    _STRATEGY_DESIGN,
    _VERDICT,
    HypothesisDevelopmentPhase,
    PhaseTransition,
    phase_from_value,
)
from .phases.trials import _draft_from_contract
from .state import HypothesisDevelopmentState

#: One transition's payload fold: typed state in, typed state out.
PayloadFold = Callable[
    [HypothesisDevelopmentState, Mapping[str, object]],
    HypothesisDevelopmentState,
]


def _phase(value: str) -> HypothesisDevelopmentPhase:
    return phase_from_value(value)


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


def _fold_discovery(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the accepted discovery contract, resetting the model artifacts.

    The selected candidate survives the reset because model creation consumes
    it; the previous attempt's model, plan, trials, and strategy artifacts do
    not.
    """
    payload = _required_payload(
        completion,
        "discovery",
        (_DISCOVERY.value, _MODEL_CREATION.value),
    )
    return replace(state, discovery=DiscoveryDraft.from_dict(payload))


def _fold_draft(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the accepted hypothesis contract as the run's draft."""
    payload = _required_payload(
        completion,
        "hypothesis",
        (_MODEL_CREATION.value, _FALSIFICATION_TRIALS.value),
    )
    return replace(state, draft=_draft_from_contract(payload, persisted=True))


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


#: The per-transition payload folds.  Unlisted transitions (the strategy
#: chain's structural moves) carry state unchanged; handlers are additive
#: follow-ups keyed by their (from_phase, to_phase) pair.
def rebuild_state(
    workspace_id: str,
    execution_id: str,
    transitions: Sequence[StoreTransition],
    *,
    initial_phase: HypothesisDevelopmentPhase = HypothesisDevelopmentPhase.DISCOVERY,
    pipes: Mapping[
        tuple[str, str],
        Callable[[HypothesisDevelopmentState], HypothesisDevelopmentState],
    ]
    | None = None,
) -> HypothesisDevelopmentState:
    """Fold an ordered transition sequence into the typed run state.

    The fold starts from the workflow's fresh state and applies each stored
    transition in order; the resulting state's phase, epoch, and transition
    records mirror the store exactly.  ``initial_phase`` is the phase a run
    rests in before its first transition: live runs start at the workflow's
    declared initial phase.
    """
    if transitions and transitions[0].from_phase == "hypothesis_creation":
        initial_phase = _MODEL_CREATION
    state = HypothesisDevelopmentState(
        workspace_id=workspace_id,
        execution_id=execution_id,
        phase=initial_phase,
        intent_epoch=0,
    )
    for transition in transitions:
        state = fold_transition(state, transition, pipes)
    return state


def fold_transition(
    state: HypothesisDevelopmentState,
    transition: StoreTransition,
    pipes: Mapping[
        tuple[str, str],
        Callable[[HypothesisDevelopmentState], HypothesisDevelopmentState],
    ]
    | None = None,
) -> HypothesisDevelopmentState:
    """Fold one stored transition into the typed state."""
    source = _phase(transition.from_phase)
    target = _phase(transition.to_phase)
    from .spec import phase_declarations, piping

    if source != state.phase or transition.sequence != len(state.transitions) + 1:
        raise WorkflowError(
            "stored transition does not follow the current phase/history"
        )
    declaration = next(
        item for item in phase_declarations() if item.spec.phase == source.value
    )
    if target.value not in declaration.edges:
        raise WorkflowError("stored transition names an undeclared edge")
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
    handler = declaration.payload_fold
    if handler is not None and target is not HypothesisDevelopmentPhase.FAILED:
        state = handler(state, transition.completion)
    declared_pipes = piping() if pipes is None else pipes
    return declared_pipes[(source.value, target.value)](state)


__all__ = [
    "fold_transition",
    "rebuild_state",
]
