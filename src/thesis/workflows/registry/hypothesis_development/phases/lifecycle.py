"""Lifecycle executors for the hypothesis-development phases.

``advance_requested`` accepts a chat request into hypothesis creation.
``run_requested_turn`` is the spec executor for the initial phase: it performs
the same multi-phase turn continuation the workflow always had by accepting the
request and then running the hypothesis-creation turn.  The retained historical
phases (``rca`` and the terminal values) are inert under the current
implementation: a run never executes a turn while resting in them, but the
registered spec still declares an executor per phase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thesis.workflows.core.workflow import Workflow

from . import _HYPOTHESIS_CREATION

if TYPE_CHECKING:
    from ..contracts.creation import HypothesisDevelopmentRequest
    from ..state import HypothesisDevelopmentState


def run_requested_turn(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Accept the request and run the creation turn (multi-phase continuation)."""
    from .creation import run_creation_turn

    state = advance_requested(workflow, request, state)
    return run_creation_turn(workflow, request, state)


def advance_requested(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Accept the chat request and enter hypothesis creation."""
    return workflow._transition(
        request, state, _HYPOTHESIS_CREATION, "chat request accepted"
    )


def advance_inert(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Return state unchanged for a phase that no longer runs turns."""
    del workflow, request
    return state


__all__ = ["advance_inert", "advance_requested", "run_requested_turn"]
