"""The discovery draft: candidate opportunity, attempts, and data posture.

Discovery is deliberately weaker than validation: the phase runs cheap,
targeted studies and hands model creation ONE candidate opportunity, every
attempt it made (changed or not), the data it deliberately left untouched, and
its honest limitations — or, when nothing is worth pursuing, the reason no
candidate was selected.  :meth:`DiscoveryDraft.from_dict` is both the rebuild
fold's decoder and the completion validator's drift guard, so a stored or
submitted payload either composes exactly here or is rejected with a
:class:`~.errors.WorkflowError`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import WorkflowError
from ..primitives import (
    _identifier,
    _text,
)

#: Text caps mirrored from the discovery completion schema.
_MAX_ATTEMPT_TEXT = 4_096
_MAX_CANDIDATE_TEXT = 4_096
_MAX_CANDIDATE_EVIDENCE = 8_192
_MAX_LIST_ITEMS = 8
_MAX_ASSUMPTION_TEXT = 2_048
_MAX_BUILDING_BLOCK_TEXT = 128
_MAX_VALIDATION_QUESTION_TEXT = 2_048


def _field(payload: Mapping[str, object], key: str) -> Any:
    """Read one canonical wire field for typed contract construction."""
    return payload.get(key)


def _text_list(
    value: object,
    label: str,
    *,
    max_items: int,
    max_length: int,
) -> tuple[str, ...]:
    """Validate one required, bounded list of non-empty text entries."""
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise WorkflowError(f"{label} must be a sequence of text entries")
    items = tuple(value)
    if not 1 <= len(items) <= max_items:
        raise WorkflowError(f"{label} must contain between 1 and {max_items} entries")
    return tuple(_text(item, f"{label} entry", max_length=max_length) for item in items)


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


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """The one opportunity discovery selected, with its competing story."""

    opportunity: str
    signal: str
    information_time: str
    direction: str
    holding_period: str
    profit_source: str
    effect: str
    competing_explanation: str
    evidence: str
    entry_idea: str
    exit_idea: str
    assumptions: tuple[str, ...]
    building_blocks: tuple[str, ...]
    validation_questions: tuple[str, ...]
    exploratory_evidence: str

    def __post_init__(self) -> None:
        for name, value in (
            ("opportunity", self.opportunity),
            ("signal", self.signal),
            ("information_time", self.information_time),
            ("direction", self.direction),
            ("holding_period", self.holding_period),
            ("profit_source", self.profit_source),
            ("effect", self.effect),
            ("competing_explanation", self.competing_explanation),
            ("entry_idea", self.entry_idea),
            ("exit_idea", self.exit_idea),
        ):
            _text(value, f"candidate {name}", max_length=_MAX_CANDIDATE_TEXT)
        _text(self.evidence, "candidate evidence", max_length=_MAX_CANDIDATE_EVIDENCE)
        _text(
            self.exploratory_evidence,
            "candidate exploratory_evidence",
            max_length=_MAX_CANDIDATE_EVIDENCE,
        )
        object.__setattr__(
            self,
            "assumptions",
            _text_list(
                self.assumptions,
                "candidate assumptions",
                max_items=_MAX_LIST_ITEMS,
                max_length=_MAX_ASSUMPTION_TEXT,
            ),
        )
        object.__setattr__(
            self,
            "building_blocks",
            _text_list(
                self.building_blocks,
                "candidate building_blocks",
                max_items=_MAX_LIST_ITEMS,
                max_length=_MAX_BUILDING_BLOCK_TEXT,
            ),
        )
        object.__setattr__(
            self,
            "validation_questions",
            _text_list(
                self.validation_questions,
                "candidate validation_questions",
                max_items=_MAX_LIST_ITEMS,
                max_length=_MAX_VALIDATION_QUESTION_TEXT,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        """Return the portable candidate representation."""
        return {
            "assumptions": list(self.assumptions),
            "buildingBlocks": list(self.building_blocks),
            "competingExplanation": self.competing_explanation,
            "direction": self.direction,
            "effect": self.effect,
            "entryIdea": self.entry_idea,
            "evidence": self.evidence,
            "exitIdea": self.exit_idea,
            "exploratoryEvidence": self.exploratory_evidence,
            "holdingPeriod": self.holding_period,
            "informationTime": self.information_time,
            "opportunity": self.opportunity,
            "profitSource": self.profit_source,
            "signal": self.signal,
            "validationQuestions": list(self.validation_questions),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DiscoveryCandidate:
        """Rebuild one candidate from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("discovery candidate must be an object")
        return cls(
            opportunity=_field(payload, "opportunity"),
            signal=_field(payload, "signal"),
            information_time=_field(payload, "informationTime"),
            direction=_field(payload, "direction"),
            holding_period=_field(payload, "holdingPeriod"),
            profit_source=_field(payload, "profitSource"),
            effect=_field(payload, "effect"),
            competing_explanation=_field(payload, "competingExplanation"),
            evidence=_field(payload, "evidence"),
            entry_idea=_field(payload, "entryIdea"),
            exit_idea=_field(payload, "exitIdea"),
            assumptions=_field(payload, "assumptions"),
            building_blocks=_field(payload, "buildingBlocks"),
            validation_questions=_field(payload, "validationQuestions"),
            exploratory_evidence=_field(payload, "exploratoryEvidence"),
        )


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

    candidate: DiscoveryCandidate | None
    no_candidate_reason: str | None
    attempts: tuple[DiscoveryAttempt, ...]
    untouched_data: str
    limitations: str

    def __post_init__(self) -> None:
        if self.candidate is not None and not isinstance(
            self.candidate, DiscoveryCandidate
        ):
            raise WorkflowError(
                "discovery draft candidate must be a DiscoveryCandidate"
            )
        if (self.candidate is None) == (self.no_candidate_reason is None):
            raise WorkflowError(
                "discovery draft requires exactly one of candidate or "
                "no_candidate_reason"
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
            "candidate": None if self.candidate is None else self.candidate.as_dict(),
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
        candidate = payload.get("candidate")
        if candidate is not None and not isinstance(candidate, Mapping):
            raise WorkflowError("discovery draft candidate must be an object")
        raw_attempts = payload.get("attempts")
        if isinstance(raw_attempts, str) or not isinstance(raw_attempts, Sequence):
            raise WorkflowError("discovery draft requires an attempts list")
        attempts = tuple(_attempt_from_dict(item) for item in raw_attempts)
        return cls(
            candidate=(
                None if candidate is None else DiscoveryCandidate.from_dict(candidate)
            ),
            no_candidate_reason=_field(payload, "noCandidateReason"),
            attempts=attempts,
            untouched_data=_field(payload, "untouchedData"),
            limitations=_field(payload, "limitations"),
        )


__all__ = [
    "DiscoveryAttempt",
    "DiscoveryCandidate",
    "DiscoveryDraft",
]
