"""Frozen cutoff attestations and the conservative cutoff check.

A cutoff attestation records what one model deployment knew and when: the
declared conservative knowledge-cutoff upper bound, the attestation identity
that signed it, the build/snapshot digest it covers, and the knowledge
aspects (training, fine-tuning, augmentation, memory, retention) whose
behavior the attestation covers.  RCA evidence claims must reference an
attestation through the registry record that served the session; a
deployment without a complete, unrevoked attestation cannot back a
cutoff-constrained session.

Attestations are frozen records with content hashes.  Revocation is held by
the :class:`AttestationRegistry` that admitted them; revocation is permanent
and never removes the record.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from thesis.registry.records import RegistryError

_SCHEMA = "fca-thesis-cutoff-attestation-v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class AttestationError(RegistryError):
    """Raised when a cutoff attestation is missing, invalid, or revoked."""


class AttestationConflictError(AttestationError):
    """An attestation identifier was reused for different content."""


class AttestedAspect(StrEnum):
    """The knowledge behavior a cutoff attestation covers."""

    TRAINING = "training"
    FINE_TUNING = "fine_tuning"
    AUGMENTATION = "augmentation"
    MEMORY = "memory"
    RETENTION = "retention"


REQUIRED_ATTESTED_ASPECTS: frozenset[AttestedAspect] = frozenset(AttestedAspect)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AttestationError(f"{label} must be a portable identifier")
    return value


def _timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise AttestationError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AttestationError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise AttestationError(f"{label} must be a calendar date")
    return value


@dataclass(frozen=True, slots=True)
class CutoffAttestation:
    """One frozen statement of what a model deployment knew and when."""

    attestation_id: str
    provider: str
    model_id: str
    cutoff_date: date
    aspects: frozenset[AttestedAspect]
    attestor: str
    attested_at: str
    snapshot_sha256: str
    schema: str = _SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.attestation_id, "attestation_id")
        _identifier(self.provider, "provider")
        _identifier(self.model_id, "model_id")
        object.__setattr__(
            self, "cutoff_date", _iso_date(self.cutoff_date, "cutoff_date")
        )
        if isinstance(self.aspects, str) or not isinstance(
            self.aspects, set | frozenset | tuple | list
        ):
            raise AttestationError("aspects must be a collection of AttestedAspect")
        aspects = frozenset(self.aspects)
        if not aspects <= set(AttestedAspect):
            raise AttestationError("aspects contains an unsupported aspect")
        if not REQUIRED_ATTESTED_ASPECTS <= aspects:
            missing = sorted(
                aspect.value for aspect in REQUIRED_ATTESTED_ASPECTS - aspects
            )
            raise AttestationError(
                f"attestation must cover every knowledge aspect; missing: {missing}"
            )
        object.__setattr__(self, "aspects", aspects)
        _identifier(self.attestor, "attestor")
        object.__setattr__(
            self, "attested_at", _timestamp(self.attested_at, "attested_at")
        )
        if (
            not isinstance(self.snapshot_sha256, str)
            or _DIGEST.fullmatch(self.snapshot_sha256) is None
        ):
            raise AttestationError("snapshot_sha256 must be a lowercase SHA-256 digest")

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document that the attestation hash covers."""
        return {
            "aspects": sorted(aspect.value for aspect in self.aspects),
            "attestationId": self.attestation_id,
            "attestedAt": self.attested_at,
            "attestor": self.attestor,
            "cutoffDate": self.cutoff_date.isoformat(),
            "modelId": self.model_id,
            "provider": self.provider,
            "schema": self.schema,
            "snapshotSha256": self.snapshot_sha256,
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of this attestation."""
        return hashlib.sha256(
            json.dumps(
                self.canonical_document,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class CutoffVerification:
    """The deterministic outcome of one conservative cutoff comparison."""

    attestation: CutoffAttestation
    earliest_observation: date
    verified_cutoff: date
    cutoff_precedes_observation: bool


def verify_cutoff(
    attestation: CutoffAttestation, *, earliest_observation: date
) -> CutoffVerification:
    """Compare the attested conservative cutoff against the observation floor.

    Cutoff-constrained RCA requires the conservative cutoff upper bound to be
    strictly before the earliest model-visible observation, including warm-up
    and lookback data.  A cutoff equal to the observation date fails.
    """
    if not isinstance(attestation, CutoffAttestation):
        raise AttestationError("attestation must be a CutoffAttestation")
    observation = _iso_date(earliest_observation, "earliest_observation")
    return CutoffVerification(
        attestation=attestation,
        earliest_observation=observation,
        verified_cutoff=attestation.cutoff_date,
        cutoff_precedes_observation=attestation.cutoff_date < observation,
    )


class AttestationRegistry:
    """Frozen cutoff attestations with permanent, explicit revocation."""

    def __init__(self) -> None:
        self._attestations: dict[str, CutoffAttestation] = {}
        self._revoked: set[str] = set()

    def register(self, attestation: CutoffAttestation) -> CutoffAttestation:
        """Admit an attestation, or return the identical admitted record.

        Re-registering identical content is idempotent; re-using an
        attestation ID for different content is rejected.  Revoked IDs can
        never be admitted again.
        """
        if not isinstance(attestation, CutoffAttestation):
            raise AttestationError("attestation must be a CutoffAttestation")
        if attestation.attestation_id in self._revoked:
            raise AttestationConflictError(
                f"attestation {attestation.attestation_id!r} is revoked"
            )
        existing = self._attestations.get(attestation.attestation_id)
        if existing is not None:
            if existing.content_sha256 == attestation.content_sha256:
                return existing
            raise AttestationConflictError(
                f"attestation ID {attestation.attestation_id!r} is already recorded"
            )
        self._attestations[attestation.attestation_id] = attestation
        return attestation

    def get(self, attestation_id: str) -> CutoffAttestation | None:
        """Return the attestation for an ID, or ``None``."""
        return self._attestations.get(attestation_id)

    def attestations(self) -> tuple[CutoffAttestation, ...]:
        """Return all admitted attestations ordered by ID."""
        return tuple(
            self._attestations[attestation_id]
            for attestation_id in sorted(self._attestations)
        )

    def is_revoked(self, attestation_id: str) -> bool:
        """Return whether an attestation ID has been revoked."""
        return attestation_id in self._revoked

    def revoke(self, attestation_id: str) -> CutoffAttestation:
        """Revoke an admitted attestation permanently."""
        attestation = self._attestations.get(attestation_id)
        if attestation is None:
            raise AttestationError(f"unknown attestation: {attestation_id!r}")
        self._revoked.add(attestation_id)
        return attestation

    def active_for(self, provider: str, model_id: str) -> tuple[CutoffAttestation, ...]:
        """Return the unrevoked attestations for one exact model identity."""
        return tuple(
            attestation
            for attestation_id in sorted(self._attestations)
            if attestation_id not in self._revoked
            and (attestation := self._attestations[attestation_id]).provider == provider
            and attestation.model_id == model_id
        )
