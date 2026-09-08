"""Revision handoff for a diagnosis that consumed a protected result.

A revision triggered by an RCA diagnosis never returns to the consumed
protected result.  The handoff document produced here records that a
protected result was consumed for diagnosis and carries the explicit
reuse and adaptation modifiers the architecture requires: the revision
may target exploratory or historical-validation data only, sealed-window
reuse is barred, full-result RCA adaptation against a window receives the
strongest selection/reuse modifier, and every adaptation starts a new
strategy lineage and contract version.  The consumed response and release
are referenced by content digest, never by content.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from thesis.experiments.grades import EvidenceGrade, EvidenceModifier
from thesis.rca.session import (
    RcaHandoffError,
    RcaResponseVerdict,
    _canonical_json,
    _timestamp,
)

REVISION_HANDOFF_SCHEMA = "fca-thesis-rca-revision-handoff-v1"

#: The grades a revision may target: exploratory or historical-validation
#: data only.  Sealed windows and forward observation are barred.
ALLOWED_REVISION_GRADES: frozenset[EvidenceGrade] = frozenset(
    {EvidenceGrade.EXPLORATORY, EvidenceGrade.HISTORICAL_VALIDATION}
)
BARRED_REVISION_GRADES: tuple[EvidenceGrade, ...] = (
    EvidenceGrade.SEALED_HISTORICAL,
    EvidenceGrade.FORWARD_OBSERVATION,
)

#: The strongest selection/reuse modifier for full-result RCA adaptation
#: against a consumed window (architecture: "RCA And Iteration").
STRONGEST_REUSE_MODIFIER = "strongest-selection-reuse-modifier"

_REVISION_MODIFIERS: tuple[EvidenceModifier, ...] = (
    EvidenceModifier.ADAPTIVE_SELECTION,
    EvidenceModifier.REUSED,
    EvidenceModifier.TRANSFORMED,
)


class RevisionHandoffError(RcaHandoffError):
    """A revision handoff would reuse protected evidence outside its rules."""


@dataclass(frozen=True, slots=True)
class RevisionHandoffDocument:
    """The returned handoff document for an RCA-triggered revision."""

    release_id: str
    rca_session_id: str
    evidence_tier: str
    target_grade: EvidenceGrade
    diagnosis_response_sha256: str
    release_content_sha256: str
    recorded_at: str | None = None
    schema: str = REVISION_HANDOFF_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.release_id, str) or not self.release_id:
            raise RevisionHandoffError("release_id must be a non-empty string")
        if not isinstance(self.rca_session_id, str) or not self.rca_session_id:
            raise RevisionHandoffError("rca_session_id must be a non-empty string")
        if not isinstance(self.evidence_tier, str) or not self.evidence_tier:
            raise RevisionHandoffError("evidence_tier must be a non-empty string")
        if not isinstance(self.target_grade, EvidenceGrade):
            raise RevisionHandoffError("target_grade must be an EvidenceGrade")
        if self.target_grade not in ALLOWED_REVISION_GRADES:
            raise RevisionHandoffError(
                "revision target grade must be exploratory or "
                "historical_validation; sealed-window reuse is barred"
            )
        if not isinstance(self.diagnosis_response_sha256, str) or not (
            self.diagnosis_response_sha256
        ):
            raise RevisionHandoffError(
                "diagnosis_response_sha256 must be a non-empty digest reference"
            )
        if not isinstance(self.release_content_sha256, str) or not (
            self.release_content_sha256
        ):
            raise RevisionHandoffError(
                "release_content_sha256 must be a non-empty digest reference"
            )
        if self.recorded_at is not None:
            object.__setattr__(
                self, "recorded_at", _timestamp(self.recorded_at, "recorded_at")
            )

    @property
    def allowed_grades(self) -> tuple[EvidenceGrade, ...]:
        """The grades the revision may use, in canonical order."""
        return tuple(sorted(ALLOWED_REVISION_GRADES, key=lambda grade: grade.value))

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this revision handoff."""
        document: dict[str, object] = {
            "adaptation": {
                "requiresNewContractVersion": True,
                "requiresNewLineageVersion": True,
            },
            "allowedGrades": [grade.value for grade in self.allowed_grades],
            "barredGrades": [grade.value for grade in BARRED_REVISION_GRADES],
            "consumed": {
                "consumedForDiagnosis": True,
                "diagnosisResponseSha256": self.diagnosis_response_sha256,
                "protectedResult": True,
                "releaseContentSha256": self.release_content_sha256,
            },
            "consumedEvidenceTier": self.evidence_tier,
            "evidenceModifiers": [modifier.value for modifier in _REVISION_MODIFIERS],
            "releaseId": self.release_id,
            "reuseModifiers": {
                "fullResultRcaAdaptation": STRONGEST_REUSE_MODIFIER,
                "sealedWindowReuse": "barred",
                "targetGrade": self.target_grade.value,
            },
            "rcaSessionId": self.rca_session_id,
            "schema": self.schema,
        }
        if self.recorded_at is not None:
            document["recordedAt"] = self.recorded_at
        return document

    @property
    def content_sha256(self) -> str:
        """Return the content digest of the canonical handoff document."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()


class RevisionHandoff:
    """The revision handoff service for consumed protected results."""

    @staticmethod
    def from_accepted_response(
        accepted: RcaResponseVerdict,
        *,
        target_grade: EvidenceGrade,
        recorded_at: str | None = None,
    ) -> RevisionHandoffDocument:
        """Build the revision handoff for one accepted RCA response.

        The handoff records that the protected result behind the accepted
        response was consumed for diagnosis and returns the revision to
        exploratory or historical-validation data only, with sealed-window
        reuse barred and the strongest selection/reuse modifier applied to
        any full-result adaptation.  A rejected response or a response
        without release linkage fails closed.
        """
        if not isinstance(accepted, RcaResponseVerdict):
            raise RevisionHandoffError("accepted must be an RcaResponseVerdict")
        if not accepted.accepted or accepted.response is None:
            raise RevisionHandoffError(
                "a rejected RCA response cannot start a revision handoff"
            )
        if not accepted.release_id or not accepted.rca_session_id:
            raise RevisionHandoffError(
                "the accepted response carries no release linkage; record it "
                "against a handoff bundle first"
            )
        if not isinstance(target_grade, EvidenceGrade):
            raise RevisionHandoffError("target_grade must be an EvidenceGrade")
        if target_grade not in ALLOWED_REVISION_GRADES:
            raise RevisionHandoffError(
                "revision target grade must be exploratory or "
                "historical_validation; sealed-window reuse is barred"
            )
        normalized_at: str | None
        if recorded_at is None:
            normalized_at = None
        else:
            try:
                normalized_at = _timestamp(recorded_at, "recorded_at")
            except RcaHandoffError as error:
                raise RevisionHandoffError(str(error)) from error
        return RevisionHandoffDocument(
            release_id=accepted.release_id,
            rca_session_id=accepted.rca_session_id,
            evidence_tier=accepted.evidence_tier,
            target_grade=target_grade,
            diagnosis_response_sha256=accepted.response_sha256,
            release_content_sha256=accepted.release_content_sha256,
            recorded_at=normalized_at,
        )


__all__ = [
    "ALLOWED_REVISION_GRADES",
    "BARRED_REVISION_GRADES",
    "REVISION_HANDOFF_SCHEMA",
    "STRONGEST_REUSE_MODIFIER",
    "RevisionHandoff",
    "RevisionHandoffDocument",
    "RevisionHandoffError",
]
