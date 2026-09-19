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
    """Clear the refuted premise's derived artifacts for a fresh proposal.

    Applied when the verdict circles back to hypothesis creation: the
    refuted hypothesis, its model references, plan, trials, and the abandoned
    strategy artifacts all belong to the previous attempt.
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
        strategy_bundle=None,
        exit_criteria=(),
        strategy_backtest=None,
        optimization_backtest=None,
    )


def reset_backtests(
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Clear the previous revision's backtest results before a redesign."""
    return replace(
        state,
        strategy_backtest=None,
        optimization_backtest=None,
    )


__all__ = [
    "HypothesisDevelopmentState",
    "identity_pipe",
    "reset_backtests",
    "reset_premise",
]
