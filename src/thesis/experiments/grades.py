"""Evidence grades, exposure modifiers, and one-way downgrades.

Thesis grades evidence by what was hidden from which actor, not by profitability:
``exploratory``, ``historical_validation``, ``sealed_historical``, and
``forward_observation``.  The ``exploratory`` value matches the
``evidenceGrade`` field of the ``fca-thesis-evidence-report-v1`` workflow
report, so workflow reports and evidence claims share one vocabulary.

Grades are not a universal scalar ranking.  Downgrades are therefore encoded
as an explicit, deterministic transition table keyed by exposure kind rather
than by a numeric order.  There is no upgrade path: adaptive reuse and
steering after result exposure downgrade evidence, and nothing in this module
can restore sealed status.  New aliases or an older RCA model do not either.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

_SCHEMA = "fca-thesis-evidence-claim-v1"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class EvidenceGradeError(ValueError):
    """Raised when an evidence grade would change outside the downgrade table."""


class EvidenceGrade(StrEnum):
    """The exposure conditions of one piece of evidence."""

    EXPLORATORY = "exploratory"
    HISTORICAL_VALIDATION = "historical_validation"
    SEALED_HISTORICAL = "sealed_historical"
    FORWARD_OBSERVATION = "forward_observation"


class EvidenceModifier(StrEnum):
    """Recorded limitations attached to a claim alongside its grade."""

    REUSED = "reused"
    SELECTION_EXPOSED = "selection-exposed"
    KNOWLEDGE_EXPOSED = "knowledge-exposed"
    INFLUENCE_UNKNOWN = "influence-unknown"
    TRANSFORMED = "transformed"
    UNDERPOWERED = "underpowered"
    ADAPTIVE_SELECTION = "adaptive-selection"
    UNKNOWN_PRIOR_EXPOSURE = "unknown-prior-exposure"


class ExposureKind(StrEnum):
    """The recorded exposure events that force grade transitions."""

    ADAPTIVE_REUSE = "adaptive-reuse"
    STEERING_AFTER_EXPOSURE = "steering-after-exposure"
    SELECTION_EXPOSURE = "selection-exposure"
    KNOWLEDGE_EXPOSURE = "knowledge-exposure"
    INFLUENCE_UNKNOWN = "influence-unknown"
    TRANSFORMATION = "transformation"
    UNKNOWN_PRIOR_EXPOSURE = "unknown-prior-exposure"


DOWNGRADE_TARGETS: Mapping[
    ExposureKind, Mapping[EvidenceGrade, EvidenceGrade | None]
] = {
    ExposureKind.ADAPTIVE_REUSE: {
        EvidenceGrade.SEALED_HISTORICAL: EvidenceGrade.EXPLORATORY,
        EvidenceGrade.FORWARD_OBSERVATION: EvidenceGrade.EXPLORATORY,
        EvidenceGrade.HISTORICAL_VALIDATION: EvidenceGrade.EXPLORATORY,
        EvidenceGrade.EXPLORATORY: None,
    },
    ExposureKind.STEERING_AFTER_EXPOSURE: {
        EvidenceGrade.SEALED_HISTORICAL: EvidenceGrade.EXPLORATORY,
        EvidenceGrade.FORWARD_OBSERVATION: EvidenceGrade.EXPLORATORY,
        EvidenceGrade.HISTORICAL_VALIDATION: EvidenceGrade.EXPLORATORY,
        EvidenceGrade.EXPLORATORY: None,
    },
    ExposureKind.SELECTION_EXPOSURE: {
        EvidenceGrade.SEALED_HISTORICAL: EvidenceGrade.EXPLORATORY,
        EvidenceGrade.FORWARD_OBSERVATION: EvidenceGrade.EXPLORATORY,
        EvidenceGrade.HISTORICAL_VALIDATION: EvidenceGrade.EXPLORATORY,
        EvidenceGrade.EXPLORATORY: None,
    },
    ExposureKind.KNOWLEDGE_EXPOSURE: {
        EvidenceGrade.SEALED_HISTORICAL: EvidenceGrade.HISTORICAL_VALIDATION,
        EvidenceGrade.FORWARD_OBSERVATION: None,
        EvidenceGrade.HISTORICAL_VALIDATION: None,
        EvidenceGrade.EXPLORATORY: None,
    },
    ExposureKind.INFLUENCE_UNKNOWN: {
        EvidenceGrade.SEALED_HISTORICAL: EvidenceGrade.HISTORICAL_VALIDATION,
        EvidenceGrade.FORWARD_OBSERVATION: None,
        EvidenceGrade.HISTORICAL_VALIDATION: None,
        EvidenceGrade.EXPLORATORY: None,
    },
    ExposureKind.TRANSFORMATION: {
        EvidenceGrade.SEALED_HISTORICAL: None,
        EvidenceGrade.FORWARD_OBSERVATION: None,
        EvidenceGrade.HISTORICAL_VALIDATION: None,
        EvidenceGrade.EXPLORATORY: None,
    },
    ExposureKind.UNKNOWN_PRIOR_EXPOSURE: {
        EvidenceGrade.SEALED_HISTORICAL: None,
        EvidenceGrade.FORWARD_OBSERVATION: None,
        EvidenceGrade.HISTORICAL_VALIDATION: None,
        EvidenceGrade.EXPLORATORY: None,
    },
}

EXPOSURE_MODIFIERS: Mapping[ExposureKind, tuple[EvidenceModifier, ...]] = {
    ExposureKind.ADAPTIVE_REUSE: (
        EvidenceModifier.REUSED,
        EvidenceModifier.ADAPTIVE_SELECTION,
    ),
    ExposureKind.STEERING_AFTER_EXPOSURE: (
        EvidenceModifier.SELECTION_EXPOSED,
        EvidenceModifier.ADAPTIVE_SELECTION,
    ),
    ExposureKind.SELECTION_EXPOSURE: (EvidenceModifier.SELECTION_EXPOSED,),
    ExposureKind.KNOWLEDGE_EXPOSURE: (EvidenceModifier.KNOWLEDGE_EXPOSED,),
    ExposureKind.INFLUENCE_UNKNOWN: (EvidenceModifier.INFLUENCE_UNKNOWN,),
    ExposureKind.TRANSFORMATION: (EvidenceModifier.TRANSFORMED,),
    ExposureKind.UNKNOWN_PRIOR_EXPOSURE: (EvidenceModifier.UNKNOWN_PRIOR_EXPOSURE,),
}

_ALLOWED_TRANSITIONS: Mapping[EvidenceGrade, frozenset[EvidenceGrade]] = {
    grade: frozenset(
        {
            target
            for targets in DOWNGRADE_TARGETS.values()
            for source, target in targets.items()
            if source is grade and target is not None and target is not grade
        }
    )
    for grade in EvidenceGrade
}


def check_grade_transition(previous: EvidenceGrade, candidate: EvidenceGrade) -> None:
    """Reject any proposed grade change outside the downgrade table.

    Grades cannot move upward: a claim that has lost sealed or validation
    status can never regain it through this module.
    """
    if not isinstance(previous, EvidenceGrade) or not isinstance(
        candidate, EvidenceGrade
    ):
        raise EvidenceGradeError("grades must be EvidenceGrade values")
    if candidate in _ALLOWED_TRANSITIONS[previous]:
        return
    raise EvidenceGradeError(
        f"grade transition {previous.value} -> {candidate.value} is not a "
        "permitted downgrade"
    )


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise EvidenceGradeError(f"{label} must be a safe identifier")
    return value


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise EvidenceGradeError("workspace_id must be a safe workspace identifier")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise EvidenceGradeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceGradeError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise EvidenceGradeError(f"{label} contains unsupported characters")
    return value


def _timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise EvidenceGradeError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceGradeError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise EvidenceGradeError("claim mappings must have string keys")
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise EvidenceGradeError("claim values must be JSON-shaped")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise EvidenceGradeError("claim values must be canonical JSON") from error


@dataclass(frozen=True, slots=True)
class DowngradeRecord:
    """One explicit, reasoned downgrade of an evidence claim."""

    from_grade: EvidenceGrade
    to_grade: EvidenceGrade
    exposure: ExposureKind
    reason: str
    actor: str
    recorded_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.from_grade, EvidenceGrade) or not isinstance(
            self.to_grade, EvidenceGrade
        ):
            raise EvidenceGradeError("downgrade grades must be EvidenceGrade values")
        if not isinstance(self.exposure, ExposureKind):
            raise EvidenceGradeError("exposure must be an ExposureKind")
        object.__setattr__(self, "reason", _text(self.reason, "downgrade reason"))
        object.__setattr__(self, "actor", _identifier(self.actor, "downgrade actor"))
        object.__setattr__(
            self, "recorded_at", _timestamp(self.recorded_at, "recorded_at")
        )


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    """A traceable claim whose grade can only move down the downgrade table."""

    claim_id: str
    workspace_id: str
    contract_sha256: str
    experiment_id: str
    statement: str
    grade: EvidenceGrade
    created_by: str
    created_at: str
    modifiers: tuple[EvidenceModifier, ...] = ()
    artifact_references: tuple[Mapping[str, object], ...] = ()
    limitations: tuple[str, ...] = ()
    version: int = 1
    downgrade_history: tuple[DowngradeRecord, ...] = ()
    schema: str = _SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", _identifier(self.claim_id, "claim_id"))
        object.__setattr__(self, "workspace_id", _workspace_id(self.workspace_id))
        object.__setattr__(
            self, "contract_sha256", _digest(self.contract_sha256, "contract_sha256")
        )
        object.__setattr__(
            self, "experiment_id", _identifier(self.experiment_id, "experiment_id")
        )
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        if not isinstance(self.grade, EvidenceGrade):
            raise EvidenceGradeError("grade must be an EvidenceGrade")
        object.__setattr__(
            self, "created_by", _identifier(self.created_by, "created_by")
        )
        object.__setattr__(
            self, "created_at", _timestamp(self.created_at, "created_at")
        )
        modifiers = tuple(self.modifiers)
        for modifier in modifiers:
            if not isinstance(modifier, EvidenceModifier):
                raise EvidenceGradeError("modifiers must be EvidenceModifier values")
        object.__setattr__(
            self,
            "modifiers",
            tuple(sorted(set(modifiers), key=lambda item: item.value)),
        )
        references = tuple(self.artifact_references)
        for reference in references:
            if not isinstance(reference, Mapping):
                raise EvidenceGradeError(
                    "artifact references must be JSON-shaped mappings"
                )
            _canonical_json(_jsonable(dict(reference)))
        object.__setattr__(
            self,
            "artifact_references",
            tuple(MappingProxyType(dict(reference)) for reference in references),
        )
        limitations = tuple(self.limitations)
        for limitation in limitations:
            _text(limitation, "limitation")
        object.__setattr__(
            self, "limitations", tuple(sorted(dict.fromkeys(limitations)))
        )
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise EvidenceGradeError("version must be an integer")
        if self.version < 1:
            raise EvidenceGradeError("version must be at least one")
        history = tuple(self.downgrade_history)
        for record in history:
            if not isinstance(record, DowngradeRecord):
                raise EvidenceGradeError(
                    "downgrade history must contain DowngradeRecord values"
                )
        object.__setattr__(self, "downgrade_history", history)

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document that the claim hash covers."""
        return {
            "artifactReferences": [
                dict(reference) for reference in self.artifact_references
            ],
            "claimId": self.claim_id,
            "contractSha256": self.contract_sha256,
            "createdAt": self.created_at,
            "createdBy": self.created_by,
            "downgradeHistory": [
                {
                    "actor": record.actor,
                    "exposure": record.exposure.value,
                    "fromGrade": record.from_grade.value,
                    "reason": record.reason,
                    "recordedAt": record.recorded_at,
                    "toGrade": record.to_grade.value,
                }
                for record in self.downgrade_history
            ],
            "experimentId": self.experiment_id,
            "grade": self.grade.value,
            "limitations": list(self.limitations),
            "modifiers": [modifier.value for modifier in self.modifiers],
            "schema": self.schema,
            "statement": self.statement,
            "version": self.version,
            "workspaceId": self.workspace_id,
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of this claim."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()


def downgrade(
    claim: EvidenceClaim,
    exposure: ExposureKind,
    *,
    reason: str,
    actor: str,
    recorded_at: str,
) -> EvidenceClaim:
    """Apply one exposure downgrade to a claim and return the new claim.

    The target grade comes from the deterministic downgrade table; a ``None``
    target keeps the current grade and records the modifier only.  The reason
    is mandatory, the version increments, and the exposure modifiers are
    unioned into the claim so reuse and steering are never silently dropped.
    """
    if not isinstance(claim, EvidenceClaim):
        raise EvidenceGradeError("claim must be an EvidenceClaim")
    if not isinstance(exposure, ExposureKind):
        raise EvidenceGradeError("exposure must be an ExposureKind")
    target = DOWNGRADE_TARGETS[exposure][claim.grade]
    new_grade = claim.grade if target is None else target
    record = DowngradeRecord(
        from_grade=claim.grade,
        to_grade=new_grade,
        exposure=exposure,
        reason=reason,
        actor=actor,
        recorded_at=recorded_at,
    )
    modifiers = set(claim.modifiers) | set(EXPOSURE_MODIFIERS[exposure])
    return EvidenceClaim(
        claim_id=claim.claim_id,
        workspace_id=claim.workspace_id,
        contract_sha256=claim.contract_sha256,
        experiment_id=claim.experiment_id,
        statement=claim.statement,
        grade=new_grade,
        created_by=claim.created_by,
        created_at=claim.created_at,
        modifiers=tuple(modifiers),
        artifact_references=claim.artifact_references,
        limitations=claim.limitations,
        version=claim.version + 1,
        downgrade_history=claim.downgrade_history + (record,),
        schema=claim.schema,
    )


__all__ = [
    "DOWNGRADE_TARGETS",
    "DowngradeRecord",
    "EXPOSURE_MODIFIERS",
    "EvidenceClaim",
    "EvidenceGrade",
    "EvidenceGradeError",
    "EvidenceModifier",
    "ExposureKind",
    "check_grade_transition",
    "downgrade",
]
