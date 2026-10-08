"""One fresh-subagent root-cause diagnosis of a failed trial."""

from __future__ import annotations

from collections.abc import Mapping
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

    @classmethod
    def from_dict(cls, payload: object) -> RcaDiagnosis:
        """Rebuild one diagnosis from the completion's ``diagnosis`` object."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("a diagnosis must be an object")
        falsifier_id = payload.get("falsifierId")
        failure_class = payload.get("failureClass")
        rationale = payload.get("rationale")
        confidence = payload.get("confidence")
        follow_ups = payload.get("followUps", [])
        if (
            not isinstance(falsifier_id, str)
            or not isinstance(failure_class, str)
            or not isinstance(rationale, str)
        ):
            raise WorkflowError(
                "a diagnosis requires falsifierId, failureClass, and rationale text"
            )
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise WorkflowError("a diagnosis confidence must be a number")
        if not isinstance(follow_ups, list) or any(
            not isinstance(item, str) for item in follow_ups
        ):
            raise WorkflowError("a diagnosis followUps must be a list of text")
        return cls(
            falsifier_id=falsifier_id,
            failure_class=failure_class,
            rationale=rationale,
            confidence=float(confidence),
            follow_ups=tuple(follow_ups),
        )
