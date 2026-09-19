"""One fresh-subagent root-cause diagnosis of a failed trial."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import (
    WorkflowError,
)
from ..primitives import (
    _identifier,
    _text,
)


@dataclass(frozen=True, slots=True)
class RcaDiagnosis:
    """One fresh-subagent root-cause diagnosis of a failed trial."""

    falsifier_id: str
    failure_class: str
    rationale: str
    confidence: float
    follow_ups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.falsifier_id, "falsifier_id")
        _text(self.failure_class, "failure_class", max_length=64)
        _text(self.rationale, "rca rationale", max_length=4_096)
        if not 0.0 <= self.confidence <= 1.0:
            raise WorkflowError("rca confidence must fall within [0, 1]")

    def as_dict(self) -> dict[str, object]:
        """Return the portable diagnosis representation."""
        return {
            "confidence": self.confidence,
            "failureClass": self.failure_class,
            "falsifierId": self.falsifier_id,
            "followUps": list(self.follow_ups),
            "rationale": self.rationale,
        }
