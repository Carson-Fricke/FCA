"""Transition validation and application.

The transition table is the only authority on phase legality.  The machine
validates a :class:`~thesis.workflows.core.spec.TransitionRequest` against the
table, stamps the request's intent epoch and the next sequence number onto a
workflow-authored transition record, and rebuilds the state.  It knows no
workflow-specific phase values or record shapes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import cast

from thesis.workflows.core.spec import TransitionRequest
from thesis.workflows.core.state import WorkflowState


class TransitionTable[PhaseT]:
    """The allowed-target sets for every phase of one workflow."""

    def __init__(
        self,
        transitions: Mapping[PhaseT, frozenset[PhaseT]],
        *,
        error: Callable[[PhaseT, PhaseT], Exception],
    ) -> None:
        self._transitions = dict(transitions)
        self._error = error

    def allowed(self, current: PhaseT) -> frozenset[PhaseT]:
        """Return the phases reachable from ``current`` in one transition."""
        return self._transitions.get(current, frozenset())

    def permits(self, current: PhaseT, target: PhaseT) -> bool:
        """Return whether ``current`` may move directly to ``target``."""
        return target in self.allowed(current)

    def validate(self, current: PhaseT, target: PhaseT) -> PhaseT:
        """Return ``target`` when legal, otherwise raise the table's error."""
        if not self.permits(current, target):
            raise self._error(current, target)
        return target


class TransitionMachine[PhaseT, RecordT, StateT: WorkflowState]:
    """Validate a transition request and apply it to one state."""

    def __init__(
        self,
        table: TransitionTable[PhaseT],
        *,
        record_of: Callable[[int, PhaseT, PhaseT, datetime, int, str], RecordT],
        rebuild: Callable[[StateT, PhaseT, tuple[RecordT, ...]], StateT],
    ) -> None:
        self._table = table
        self._record_of = record_of
        self._rebuild = rebuild

    def apply(
        self,
        state: StateT,
        request: TransitionRequest[PhaseT],
        *,
        reason: str,
        at: datetime,
    ) -> StateT:
        """Validate the request, record the transition, and return new state."""
        current = cast(PhaseT, state.phase)
        target = self._table.validate(current, request.target_phase)
        records = cast(tuple[RecordT, ...], state.transitions)
        transition = self._record_of(
            len(records) + 1,
            current,
            target,
            at,
            request.intent_epoch,
            reason,
        )
        return self._rebuild(state, target, (*records, transition))
