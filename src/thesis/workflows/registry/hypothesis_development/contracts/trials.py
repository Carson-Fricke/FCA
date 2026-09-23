"""One recorded falsifier trial with bound run evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..errors import (
    WorkflowError,
)
from ..primitives import (
    _DIGEST,
    _identifier,
    _text,
)


@dataclass(frozen=True, slots=True)
class FalsificationTrial:
    """One recorded falsifier trial with bound run evidence."""

    test_id: str
    falsifier_id: str
    run_id: str
    digest: str
    run_state: str
    observed: float | None
    falsified: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.test_id, "test_id")
        _identifier(self.falsifier_id, "falsifier_id")
        _text(self.run_id, "run_id", max_length=256)
        if not isinstance(self.digest, str) or _DIGEST.fullmatch(self.digest) is None:
            raise WorkflowError("trial digest must be a lowercase SHA-256 digest")
        _text(self.run_state, "run_state", max_length=64)
        if self.observed is not None and (
            isinstance(self.observed, bool)
            or not isinstance(self.observed, int | float)
        ):
            raise WorkflowError("trial observation must be numeric when present")
        if self.detail is not None:
            _text(self.detail, "trial detail", max_length=4_096)

    def as_dict(self) -> dict[str, object]:
        """Return the portable trial representation."""
        return {
            "detail": self.detail,
            "digest": self.digest,
            "falsified": self.falsified,
            "falsifierId": self.falsifier_id,
            "observed": self.observed,
            "runId": self.run_id,
            "runState": self.run_state,
            "testId": self.test_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FalsificationTrial:
        """Rebuild one trial from its portable representation."""
        observed = payload.get("observed")
        detail = payload.get("detail")
        return cls(
            test_id=str(payload.get("testId", "")),
            falsifier_id=str(payload.get("falsifierId", "")),
            run_id=str(payload.get("runId", "")),
            digest=str(payload.get("digest", "")),
            run_state=str(payload.get("runState", "")),
            observed=(
                observed
                if isinstance(observed, int | float) and not isinstance(observed, bool)
                else None
            ),
            falsified=bool(payload.get("falsified", False)),
            detail=None if detail is None else str(detail),
        )
