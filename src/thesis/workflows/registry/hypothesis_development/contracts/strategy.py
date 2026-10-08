"""Typed strategy-chain handoffs: the frozen program reference and the backtest.

The strategy design phase freezes one committed program reference.  Every later
phase that runs or reports that strategy must name the same reference, and the
strategy backtest handoff records the single harness run that tested it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..errors import WorkflowError
from ..primitives import _text

#: The terminal outcomes an accepted strategy backtest may report.  A running
#: or queued run is not a handoff: the phase waits for a terminal status.
STRATEGY_BACKTEST_STATES = frozenset(("succeeded", "failed", "cancelled", "unknown"))

_REFERENCE_KEYS = ("workspaceId", "kind", "programId", "gitCommit", "version")


def program_reference(payload: object) -> dict[str, object]:
    """Return the frozen strategy reference a receipt or handoff names.

    Only identity fields are kept: the reference is the committed program
    identity, not a copy of the receipt's file listing or validation output.
    """
    if not isinstance(payload, Mapping):
        raise WorkflowError("a strategy reference must be an object")
    reference: dict[str, object] = {}
    for key in _REFERENCE_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError(f"strategy reference requires a non-empty {key}")
        reference[key] = value
    if reference["kind"] != "strategy":
        raise WorkflowError("strategy reference must name a strategy program")
    return reference


@dataclass(frozen=True, slots=True)
class StrategyBacktest:
    """The accepted strategy backtest: one harness run of the frozen strategy."""

    reference: Mapping[str, object]
    job_id: str
    state: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference", program_reference(self.reference))
        _text(self.job_id, "strategy backtest job id", max_length=128)
        if self.state not in STRATEGY_BACKTEST_STATES:
            raise WorkflowError(
                f"strategy backtest state {self.state!r} is not a terminal outcome"
            )

    def as_dict(self) -> dict[str, object]:
        """Return the portable backtest handoff representation."""
        return {
            "jobId": self.job_id,
            "state": self.state,
            "strategy": dict(self.reference),
        }

    @classmethod
    def from_dict(cls, payload: object) -> StrategyBacktest:
        """Rebuild one backtest handoff from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("a strategy backtest must be an object")
        job_id = payload.get("jobId")
        state = payload.get("state")
        if not isinstance(job_id, str) or not isinstance(state, str):
            raise WorkflowError("a strategy backtest requires jobId and state")
        return cls(
            reference=program_reference(payload.get("strategy")),
            job_id=job_id,
            state=state,
        )


def strategy_backtest_payload_errors(payload: Mapping[str, object]) -> list[str]:
    """Collect every structural miss in one strategy backtest handoff."""
    errors: list[str] = []
    job_id = payload.get("jobId")
    if not isinstance(job_id, str) or not job_id.strip():
        errors.append("backtest.jobId must name the retained job of this run")
    if payload.get("state") not in STRATEGY_BACKTEST_STATES:
        errors.append("backtest.state must be succeeded, failed, cancelled, or unknown")
    try:
        program_reference(payload.get("strategy"))
    except WorkflowError as error:
        errors.append(f"backtest.strategy: {error}")
    return errors


__all__ = [
    "STRATEGY_BACKTEST_STATES",
    "StrategyBacktest",
    "program_reference",
    "strategy_backtest_payload_errors",
]
