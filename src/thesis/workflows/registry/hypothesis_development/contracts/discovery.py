"""Reference-only discovery handoff and investigation history.

Discovery develops an evolving committed strategy sketch. Completion accepts
its exact revision, job-linked attempts, untouched-data posture and limitations,
or an honest no-edge conclusion. The workspace sketch remains the authoritative
research argument rather than a second candidate narrative in the phase record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from thesis.workspaces.sketch_contract import StrategySketchReference

from ..errors import WorkflowError
from ..primitives import (
    _identifier,
    _text,
)

_MAX_ATTEMPT_TEXT = 4_096
_MAX_CANDIDATE_TEXT = 4_096


def _sketch_reference(
    value: Mapping[str, object] | None,
) -> StrategySketchReference | None:
    if value is None:
        return None
    try:
        return StrategySketchReference.parse(value)
    except ValueError as error:
        raise WorkflowError(str(error)) from error


def _field(payload: Mapping[str, object], key: str) -> Any:
    """Read one canonical wire field for typed contract construction."""
    return payload.get(key)


@dataclass(frozen=True, slots=True)
class DiscoveryAttempt:
    """One study the discovery phase ran, including its negative results."""

    purpose: str
    data_types: tuple[str, ...]
    window: str
    result: str
    changed: bool = False
    job_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.purpose, "attempt purpose", max_length=_MAX_ATTEMPT_TEXT)
        if isinstance(self.data_types, str) or not isinstance(
            self.data_types, Sequence
        ):
            raise WorkflowError("attempt data_types must be a sequence")
        data_types = tuple(self.data_types)
        if not data_types:
            raise WorkflowError("attempt data_types must contain at least one type")
        for data_type in data_types:
            _identifier(data_type, "attempt data type")
        object.__setattr__(self, "data_types", data_types)
        _text(self.window, "attempt window", max_length=_MAX_ATTEMPT_TEXT)
        _text(self.result, "attempt result", max_length=_MAX_ATTEMPT_TEXT)
        if not isinstance(self.changed, bool):
            raise WorkflowError("attempt changed must be a boolean")
        if self.job_id is not None:
            _identifier(self.job_id, "attempt job_id")

    def as_dict(self) -> dict[str, object]:
        """Return the portable attempt representation."""
        return {
            "changed": self.changed,
            "dataTypes": list(self.data_types),
            "jobId": self.job_id,
            "purpose": self.purpose,
            "result": self.result,
            "window": self.window,
        }


def _attempt_from_dict(payload: object) -> DiscoveryAttempt:
    if not isinstance(payload, Mapping):
        raise WorkflowError("discovery attempt must be an object")
    data_types = _field(payload, "dataTypes")
    if isinstance(data_types, str) or not isinstance(data_types, Sequence):
        raise WorkflowError("discovery attempt requires dataTypes")
    return DiscoveryAttempt(
        purpose=_field(payload, "purpose"),
        data_types=tuple(data_types),
        window=_field(payload, "window"),
        result=_field(payload, "result"),
        changed=_field(payload, "changed") if "changed" in payload else False,
        job_id=_field(payload, "jobId"),
    )


@dataclass(frozen=True, slots=True)
class DiscoveryDraft:
    """The accepted discovery handoff: candidate or no-candidate reason."""

    sketch_reference: StrategySketchReference | None
    no_candidate_reason: str | None
    attempts: tuple[DiscoveryAttempt, ...]
    untouched_data: str
    limitations: str

    def __post_init__(self) -> None:
        if self.sketch_reference is not None and not isinstance(
            self.sketch_reference, StrategySketchReference
        ):
            raise WorkflowError(
                "discovery sketch_reference must be a StrategySketchReference"
            )
        if (self.sketch_reference is None) == (self.no_candidate_reason is None):
            raise WorkflowError(
                "discovery requires exactly one of sketchReference or noCandidateReason"
            )
        if self.no_candidate_reason is not None:
            _text(
                self.no_candidate_reason,
                "discovery no_candidate_reason",
                max_length=_MAX_CANDIDATE_TEXT,
            )
        if isinstance(self.attempts, str) or not isinstance(self.attempts, tuple):
            raise WorkflowError("discovery draft attempts must be a tuple")
        if not self.attempts:
            raise WorkflowError("discovery draft requires at least one attempt")
        if any(not isinstance(attempt, DiscoveryAttempt) for attempt in self.attempts):
            raise WorkflowError(
                "discovery draft attempts must be DiscoveryAttempt values"
            )
        _text(
            self.untouched_data,
            "discovery untouched_data",
            max_length=_MAX_ATTEMPT_TEXT,
        )
        _text(self.limitations, "discovery limitations", max_length=_MAX_ATTEMPT_TEXT)

    def as_dict(self) -> dict[str, object]:
        """Return the portable discovery draft representation."""
        return {
            "attempts": [attempt.as_dict() for attempt in self.attempts],
            "sketchReference": None
            if self.sketch_reference is None
            else self.sketch_reference.document(),
            "limitations": self.limitations,
            "noCandidateReason": self.no_candidate_reason,
            "untouchedData": self.untouched_data,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DiscoveryDraft:
        """Rebuild one discovery draft from its portable representation.

        Malformed payloads never partially compose: every missing or
        wrongly-typed field raises :class:`~.errors.WorkflowError`.
        """
        if not isinstance(payload, Mapping):
            raise WorkflowError("discovery draft must be an object")
        reference = payload.get("sketchReference")
        if reference is not None and not isinstance(reference, Mapping):
            raise WorkflowError("discovery sketchReference must be an object")
        raw_attempts = payload.get("attempts")
        if isinstance(raw_attempts, str) or not isinstance(raw_attempts, Sequence):
            raise WorkflowError("discovery draft requires an attempts list")
        attempts = tuple(_attempt_from_dict(item) for item in raw_attempts)
        return cls(
            sketch_reference=_sketch_reference(reference),
            no_candidate_reason=_field(payload, "noCandidateReason"),
            attempts=attempts,
            untouched_data=_field(payload, "untouchedData"),
            limitations=_field(payload, "limitations"),
        )


__all__ = [
    "DiscoveryAttempt",
    "DiscoveryDraft",
]
