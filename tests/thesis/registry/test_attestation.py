from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast

import pytest

from thesis.registry.attestation import (
    AttestationConflictError,
    AttestationError,
    AttestationRegistry,
    AttestedAspect,
    CutoffAttestation,
    verify_cutoff,
)

ATTESTED_AT = "2024-05-01T00:00:00.000000Z"


def _attestation(**overrides: object) -> CutoffAttestation:
    base = CutoffAttestation(
        attestation_id="attest-2024-04",
        provider="azure_ai_foundry",
        model_id="gpt-4.1",
        cutoff_date=date(2024, 4, 1),
        aspects=frozenset(AttestedAspect),
        attestor="provider-attestation-pipeline",
        attested_at=ATTESTED_AT,
        snapshot_sha256="c" * 64,
    )
    if not overrides:
        return base
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_attestation_hashes_canonically_and_covers_every_aspect() -> None:
    attestation = _attestation()
    again = _attestation()
    assert attestation.content_sha256 == again.content_sha256
    document = attestation.canonical_document
    assert document["schema"] == "fca-thesis-cutoff-attestation-v1"
    assert document["cutoffDate"] == "2024-04-01"
    assert document["attestor"] == "provider-attestation-pipeline"
    assert document["snapshotSha256"] == "c" * 64
    assert document["aspects"] == [
        "augmentation",
        "fine_tuning",
        "memory",
        "retention",
        "training",
    ]


def test_attestation_requires_every_knowledge_aspect() -> None:
    with pytest.raises(AttestationError):
        _attestation(aspects=frozenset(AttestedAspect) - {AttestedAspect.MEMORY})
    with pytest.raises(AttestationError):
        _attestation(aspects=frozenset({AttestedAspect.TRAINING}))
    with pytest.raises(AttestationError):
        _attestation(aspects=frozenset({"training", "memory"}))


def test_attestation_rejects_invalid_identity_fields() -> None:
    with pytest.raises(AttestationError):
        _attestation(attestation_id="")
    with pytest.raises(AttestationError):
        _attestation(model_id="")
    with pytest.raises(AttestationError):
        _attestation(cutoff_date=datetime(2024, 4, 1, tzinfo=UTC))
    with pytest.raises(AttestationError):
        _attestation(cutoff_date="2024-04-01")
    with pytest.raises(AttestationError):
        _attestation(snapshot_sha256="C" * 64)
    with pytest.raises(AttestationError):
        _attestation(snapshot_sha256="short")
    with pytest.raises(AttestationError):
        _attestation(attested_at="2024-05-01")


def test_attestation_registry_register_is_idempotent() -> None:
    registry = AttestationRegistry()
    first = registry.register(_attestation())
    second = registry.register(_attestation())
    assert first is second
    assert registry.get("attest-2024-04") is first
    assert registry.get("missing") is None
    assert registry.attestations() == (first,)


def test_attestation_registry_rejects_conflicting_content() -> None:
    registry = AttestationRegistry()
    registry.register(_attestation())
    with pytest.raises(AttestationConflictError):
        registry.register(_attestation(cutoff_date=date(2024, 6, 1)))


def test_attestation_revocation_is_permanent() -> None:
    registry = AttestationRegistry()
    registry.register(_attestation())
    assert registry.is_revoked("attest-2024-04") is False
    revoked = registry.revoke("attest-2024-04")
    assert revoked.model_id == "gpt-4.1"
    assert registry.is_revoked("attest-2024-04") is True
    assert registry.active_for("azure_ai_foundry", "gpt-4.1") == ()
    with pytest.raises(AttestationConflictError):
        registry.register(_attestation())
    with pytest.raises(AttestationError):
        registry.revoke("missing")


def test_active_for_matches_exact_provider_and_model() -> None:
    registry = AttestationRegistry()
    attestation = registry.register(_attestation())
    other_model = registry.register(
        _attestation(attestation_id="attest-other", model_id="gpt-4o")
    )
    assert registry.active_for("azure_ai_foundry", "gpt-4.1") == (attestation,)
    assert registry.active_for("azure_ai_foundry", "gpt-4o") == (other_model,)
    assert registry.active_for("openai", "gpt-4.1") == ()


def test_verify_cutoff_requires_strictly_earlier_conservative_bound() -> None:
    attestation = _attestation()
    early = verify_cutoff(attestation, earliest_observation=date(2024, 4, 2))
    assert early.cutoff_precedes_observation is True
    assert early.verified_cutoff == date(2024, 4, 1)
    same_day = verify_cutoff(attestation, earliest_observation=date(2024, 4, 1))
    assert same_day.cutoff_precedes_observation is False
    later = verify_cutoff(attestation, earliest_observation=date(2024, 3, 31))
    assert later.cutoff_precedes_observation is False
    with pytest.raises(AttestationError):
        verify_cutoff(
            attestation,
            earliest_observation=cast("date", "2024-04-02"),
        )
