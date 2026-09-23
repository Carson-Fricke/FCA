"""Falsifier-bound strategy exit criteria."""

from __future__ import annotations

from dataclasses import dataclass

from thesis.research.hypothesis import FALSIFIER_CONDITIONS

from ..errors import (
    WorkflowError,
)
from ..primitives import (
    _identifier,
    _text,
)


@dataclass(frozen=True, slots=True)
class ExitCriterion:
    """A strategy exit phrased as a falsifier re-check."""

    observable: str
    condition: str
    threshold: float
    description: str
    falsifier_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.observable, "exit observable")
        if self.condition not in FALSIFIER_CONDITIONS:
            raise WorkflowError(
                f"exit condition must be one of {sorted(FALSIFIER_CONDITIONS)}"
            )
        if isinstance(self.threshold, bool) or not isinstance(
            self.threshold, int | float
        ):
            raise WorkflowError("exit threshold must be a finite number")
        object.__setattr__(self, "threshold", float(self.threshold))
        _text(self.description, "exit description", max_length=2_048)
        if self.falsifier_id is not None:
            _identifier(self.falsifier_id, "exit falsifier_id")

    def as_dict(self) -> dict[str, object]:
        """Return the portable exit-criterion representation."""
        return {
            "condition": self.condition,
            "description": self.description,
            "falsifierId": self.falsifier_id,
            "observable": self.observable,
            "threshold": self.threshold,
        }
