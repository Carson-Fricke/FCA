"""The conservative falsification verdict for one hypothesis."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from ..errors import (
    WorkflowError,
)
from ..primitives import (
    _VERDICTS,
    VERDICT_REFUTED,
    _text,
)
from .trials import FalsificationTrial


@dataclass(frozen=True, slots=True)
class FalsificationVerdict:
    """The conservative falsification verdict for one hypothesis."""

    outcome: str
    reason: str
    trials: tuple[FalsificationTrial, ...] = ()
    evidence: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in _VERDICTS:
            raise WorkflowError(f"verdict outcome must be one of {sorted(_VERDICTS)}")
        _text(self.reason, "verdict reason", max_length=2_048)
        if self.evidence is not None:
            _text(self.evidence, "verdict evidence", max_length=8_192)

    @property
    def falsified(self) -> bool:
        """Return whether the verdict refutes the hypothesis."""
        return self.outcome == VERDICT_REFUTED

    def as_dict(self) -> dict[str, object]:
        """Return the portable verdict representation."""
        return {
            "evidence": self.evidence,
            "outcome": self.outcome,
            "reason": self.reason,
            "trials": [trial.as_dict() for trial in self.trials],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FalsificationVerdict:
        """Rebuild one verdict from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("verdict payload must be an object")
        outcome = payload.get("outcome")
        if not isinstance(outcome, str):
            raise WorkflowError("verdict outcome must be text")
        reason = payload.get("reason")
        if not isinstance(reason, str):
            raise WorkflowError("verdict reason must be text")
        evidence = payload.get("evidence")
        if evidence is not None and not isinstance(evidence, str):
            raise WorkflowError("verdict evidence must be text")
        raw_trials = payload.get("trials")
        if raw_trials is None:
            trials: tuple[FalsificationTrial, ...] = ()
        elif isinstance(raw_trials, str) or not isinstance(raw_trials, Sequence):
            raise WorkflowError("verdict trials must be a list")
        else:
            trials = tuple(
                FalsificationTrial.from_dict(cast(Mapping[str, object], item))
                for item in raw_trials
            )
        return cls(
            outcome=outcome,
            reason=reason,
            trials=trials,
            evidence=evidence,
        )


__all__ = [
    "FalsificationVerdict",
]
