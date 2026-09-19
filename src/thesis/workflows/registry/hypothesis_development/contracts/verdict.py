"""The conservative falsification verdict for one hypothesis."""

from __future__ import annotations

from dataclasses import dataclass

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

    def __post_init__(self) -> None:
        if self.outcome not in _VERDICTS:
            raise WorkflowError(f"verdict outcome must be one of {sorted(_VERDICTS)}")
        _text(self.reason, "verdict reason", max_length=2_048)

    @property
    def falsified(self) -> bool:
        """Return whether the verdict refutes the hypothesis."""
        return self.outcome == VERDICT_REFUTED

    def as_dict(self) -> dict[str, object]:
        """Return the portable verdict representation."""
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "trials": [trial.as_dict() for trial in self.trials],
        }
