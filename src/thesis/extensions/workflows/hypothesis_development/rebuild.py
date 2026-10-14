"""The execution-store rebuild fold (REQ-B4).

A pure, I/O-free fold from an ordered transition-with-completion sequence
(:class:`~thesis_runtime.workflows.execution_store.StoreTransition`) into the
workflow's typed :class:`HypothesisDevelopmentState`.  Each transition moves
the phase and carries its completion contract; the fold dispatches on
``(from_phase, to_phase)`` to apply the contract's payload to the typed state:

- the discovery→model-creation contract's ``discovery`` payload becomes the
  discovery draft, and the model artifacts are reset while keeping it,
- the model-creation→trials contract's ``hypothesis`` payload becomes the draft,
- the trials→verdict contract's ``trials`` list becomes ``state.trials``,
- verdict contracts set ``state.verdict``,
- the optimization→completed/strategy_rca contract's ``optimization`` payload
  becomes the typed candidate evaluation,
- the verdict→model-creation circle-back records the verdict and resets to a
  fresh model state per ``reset_premise`` semantics (keeping discovery),
- the verdict→discovery restart records the verdict and resets everything per
  ``reset_discovery`` semantics.

The hypothesis document side-store is deliberately not read: the fold is
store-only, so a rebuild never reaches into the workspace.  Strategy-chain
payload handlers are additive follow-ups in the dispatch table.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from thesis_runtime.workflows.execution_store import StoreTransition

from .contracts.candidates import CandidateEvaluation
from .contracts.discovery import DiscoveryDraft
from .contracts.evidence import ResearchEvidence
from .contracts.forward import ForwardEvaluation
from .contracts.nulls import NullTestEvaluation
from .contracts.rca import RcaDiagnosis
from .contracts.strategy import StrategyBacktest, program_reference
from .contracts.trials import FalsificationTrial
from .contracts.variation import VariationEvaluation
from .contracts.verdict import FalsificationVerdict
from .errors import WorkflowError
from .phases import (
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


def _fold_discovery(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the accepted discovery contract, resetting the model artifacts.

    The selected candidate (or the recorded no-candidate reason) survives the
    reset because model creation consumes it; the previous attempt's model,
    plan, trials, and strategy artifacts do not.
    """
    payload = completion.get("discovery")
    if not isinstance(payload, Mapping) or not payload:
        raise WorkflowError("the discovery completion requires a 'discovery' object")
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
    return replace(state, draft=_draft_from_contract(payload))


def _fold_trials(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the trials contract and its evidence envelope, round-tripped.

    The envelope is the trials phase's portable output: it restores the plan,
    the model and fit references, the fitted parameters, the runtime
    configuration, and the typed evidence record.
    """
    raw = completion.get("trials")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise WorkflowError("the trials→verdict completion requires a trials list")
    trials = tuple(FalsificationTrial.from_dict(item) for item in raw)
    raw_research = completion.get("research")
    if not isinstance(raw_research, Mapping) or not raw_research:
        raise WorkflowError(
            "the trials→verdict completion requires a research envelope"
        )
    evidence = ResearchEvidence.from_dict(raw_research)
    return replace(
        state,
        trials=trials,
        research_evidence=evidence,
        research_model_reference=evidence.model,
        research_model_artifact=evidence.workspace_artifact,
        fitted_model_reference=evidence.fit,
        fitted_parameters=evidence.fitted_parameters,
        research_model_runtime_configuration=evidence.runtime_configuration,
        plan=evidence.plan,
    )


def _fold_verdict(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the verdict contract as the run's verdict."""
    payload = _required_payload(
        completion,
        "verdict",
        (_VERDICT.value, _STRATEGY_DESIGN.value),
    )
    return replace(state, verdict=FalsificationVerdict.from_dict(payload))


def _fold_optimization(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the accepted candidate-evaluation contract to the typed state."""
    payload = completion.get("optimization")
    if not isinstance(payload, Mapping) or not payload:
        raise WorkflowError(
            "the optimization completion requires an 'optimization' object"
        )
    return replace(state, candidate_evaluation=CandidateEvaluation.from_dict(payload))


def _fold_null_testing(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the accepted null-testing contract to the typed state."""
    payload = completion.get("nullTesting")
    if not isinstance(payload, Mapping) or not payload:
        raise WorkflowError(
            "the null-testing completion requires a 'nullTesting' object"
        )
    return replace(state, null_testing=NullTestEvaluation.from_dict(payload))


def _fold_variation(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the accepted variation contract to the typed state."""
    payload = completion.get("variation")
    if not isinstance(payload, Mapping) or not payload:
        raise WorkflowError("the variation completion requires a 'variation' object")
    return replace(state, variation=VariationEvaluation.from_dict(payload))


def _fold_forward_validation(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the accepted forward-validation contract to the typed state."""
    payload = completion.get("forwardValidation")
    if not isinstance(payload, Mapping) or not payload:
        raise WorkflowError(
            "the forward-validation completion requires a 'forwardValidation' object"
        )
    return replace(state, forward_validation=ForwardEvaluation.from_dict(payload))


def _fold_strategy_design(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Freeze the accepted strategy reference every later phase must name."""
    payload = completion.get("strategy")
    if not isinstance(payload, Mapping) or not payload:
        raise WorkflowError(
            "the strategy design completion requires a 'strategy' object"
        )
    return replace(state, strategy=program_reference(payload))


def _fold_strategy_backtesting(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Apply the accepted strategy backtest handoff."""
    payload = completion.get("backtest")
    if not isinstance(payload, Mapping) or not payload:
        raise WorkflowError(
            "the strategy backtesting completion requires a 'backtest' object"
        )
    return replace(state, strategy_backtest=StrategyBacktest.from_dict(payload))


def _fold_strategy_rca(
    state: HypothesisDevelopmentState, completion: Mapping[str, object]
) -> HypothesisDevelopmentState:
    """Append the accepted strategy root-cause diagnosis to the run's history."""
    payload = completion.get("diagnosis")
    if not isinstance(payload, Mapping) or not payload:
        raise WorkflowError("the strategy RCA completion requires a 'diagnosis' object")
    return replace(state, rca=(*state.rca, RcaDiagnosis.from_dict(payload)))


#: The per-transition payload folds are declared on each phase's
#: ``PhaseDeclaration``.  A phase without a payload fold carries state unchanged
#: across its transitions; every completion that moves typed data has one.
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
