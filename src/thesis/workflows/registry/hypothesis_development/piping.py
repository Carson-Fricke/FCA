"""Typed phase-to-phase piping for the hypothesis-development state.

Each pipe moves the workflow's typed state across one declared transition and
is registered in :mod:`thesis.workflows.registry.hypothesis_development.spec`.
The functions are the single implementation of the state movement the phase
executors previously inlined.
"""

from __future__ import annotations

from dataclasses import replace

from .state import HypothesisDevelopmentState


def identity_pipe(state: HypothesisDevelopmentState) -> HypothesisDevelopmentState:
    """Carry state unchanged across a transition that moves no data."""
    return state


def reset_premise(
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Clear the refuted premise's model artifacts for a fresh proposal.

    Applied when the workflow circles back from the verdict to model creation
    or rediscovery to model creation: the refuted hypothesis, its model
    references, plan, trials, and the abandoned strategy artifacts all belong
    to the previous attempt.  The discovery draft is deliberately kept: the
    selected candidate is the premise the new model re-uses, and only a full
    rediscovery clears it through :func:`reset_discovery`.
    """
    return replace(
        state,
        hypothesis=None,
        draft=None,
        research_model_reference=None,
        research_model_artifact=None,
        fitted_model_reference=None,
        fitted_parameters=None,
        research_model_runtime_configuration=None,
        plan=None,
        trials=(),
        strategy=None,
        strategy_backtest=None,
        candidate_evaluation=None,
        null_testing=None,
        variation=None,
        forward_validation=None,
    )


def reset_discovery(
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Clear the whole attempt for a full restart from discovery.

    Applied when the verdict sends the run all the way back to discovery: the
    selected candidate, the drafted and finalized hypothesis, the model and
    its fit, the plan, the trials, and every strategy artifact and backtest
    are superseded.
    """
    return replace(
        state,
        discovery=None,
        hypothesis=None,
        draft=None,
        research_model_reference=None,
        research_model_artifact=None,
        fitted_model_reference=None,
        fitted_parameters=None,
        research_model_runtime_configuration=None,
        plan=None,
        trials=(),
        strategy=None,
        strategy_backtest=None,
        candidate_evaluation=None,
        null_testing=None,
        variation=None,
        forward_validation=None,
    )


def reset_backtests(
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Clear the previous revision's strategy evidence before a redesign.

    The redesign names a new frozen strategy reference, so the old reference,
    its backtest handoff, and every evaluation built on it are superseded.
    """
    return replace(
        state,
        strategy=None,
        strategy_backtest=None,
        candidate_evaluation=None,
        null_testing=None,
        variation=None,
        forward_validation=None,
    )


__all__ = [
    "HypothesisDevelopmentState",
    "identity_pipe",
    "reset_backtests",
    "reset_discovery",
    "reset_premise",
]
