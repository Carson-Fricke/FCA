"""Deterministic gate protocol and outcomes.

A gate is a zero-argument check that returns ``None`` when the phase may
advance or the concrete failure reason when it may not.  The outcome record
keeps that verdict explicit so the engine can route a blocked phase into an
operator conversation instead of failing the run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: A gate evaluates one phase exit and returns its failure reason, if any.
type GateCheck = Callable[[], str | None]


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """The verdict of one deterministic gate check."""

    reason: str | None = None

    @property
    def passed(self) -> bool:
        """Return whether the gate released the phase."""
        return self.reason is None


def evaluate_gate(check: GateCheck) -> GateOutcome:
    """Run one gate check and wrap its verdict as an outcome."""
    return GateOutcome(reason=check())
