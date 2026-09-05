from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest

from thesis.agent.router import ModelProfile
from thesis.registry.attestation import (
    AttestationRegistry,
    AttestedAspect,
    CutoffAttestation,
)
from thesis.registry.models import (
    ModelRegistry,
    ModelSession,
    ModelSessionVerifier,
    RegistryError,
    SessionLedgerEntry,
    SessionVerificationError,
    model_profile_for,
    verify_session_chain,
)
from thesis.registry.records import ModelCapability, ModelRegistryRecord
from thesis.registry.tiers import RcaEvidenceTier, TierPolicyError

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOW_TEXT = "2026-09-01T12:00:00.000000Z"
OBSERVATION = date(2024, 4, 2)


def _record(**overrides: object) -> ModelRegistryRecord:
    base = ModelRegistryRecord(
        record_id="foundry/gpt-4.1",
        version=1,
        provider="azure_ai_foundry",
        model_id="gpt-4.1",
        model_version="2024.4.9",
        deployment="rca-critique-deployment",
        context_window_tokens=128_000,
        capabilities=frozenset({ModelCapability.TEXT_GENERATION}),
        allowed_roles=frozenset({"critique", "research"}),
        immutable_weights=True,
        attestation_id="attest-2024-04",
        registered_at=NOW_TEXT,
        registered_by="registry-operator",
    )
    if not overrides:
        return base
    return replace(base, **overrides)  # type: ignore[arg-type]


def _attestation(**overrides: object) -> CutoffAttestation:
    base = CutoffAttestation(
        attestation_id="attest-2024-04",
        provider="azure_ai_foundry",
        model_id="gpt-4.1",
        cutoff_date=date(2024, 4, 1),
        aspects=frozenset(AttestedAspect),
        attestor="provider-attestation-pipeline",
        attested_at="2024-05-01T00:00:00.000000Z",
        snapshot_sha256="c" * 64,
    )
    if not overrides:
        return base
    return replace(base, **overrides)  # type: ignore[arg-type]


def _registry(**record_overrides: object) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(_record(**record_overrides))
    return registry


def _attestations() -> AttestationRegistry:
    attestations = AttestationRegistry()
    attestations.register(_attestation())
    return attestations


def _verifier(
    registry: ModelRegistry,
    attestations: AttestationRegistry,
    *,
    ids: tuple[str, ...] = ("session-1", "session-2", "session-3"),
) -> ModelSessionVerifier:
    return ModelSessionVerifier(
        registry=registry,
        attestations=attestations,
        clock=lambda: NOW,
        id_factory=iter(ids).__next__,
    )


def _open(verifier: ModelSessionVerifier, **overrides: Any) -> ModelSession:
    values: dict[str, Any] = {
        "record_id": "foundry/gpt-4.1",
        "role": "critique",
        "tier": RcaEvidenceTier.CUTOFF_CONSTRAINED,
        "workspace_scope": "workspace-a",
        "earliest_observation": OBSERVATION,
        "actor": "rca-workflow",
    }
    values.update(overrides)
    return verifier.open_session(**values)


def test_verified_session_records_identity_attestation_and_checks() -> None:
    verifier = _verifier(_registry(), _attestations())
    session = _open(verifier)
    assert session.record.session_id == "session-1"
    assert session.record.record_id == "foundry/gpt-4.1"
    assert session.record.record_version == 1
    assert session.record.record_sha256 == _record().content_sha256
    assert session.record.provider == "azure_ai_foundry"
    assert session.record.deployment == "rca-critique-deployment"
    assert session.record.attestation_id == "attest-2024-04"
    assert session.record.attestation_sha256 == _attestation().content_sha256
    assert session.record.verified_cutoff == "2024-04-01"
    assert session.record.earliest_observation == "2024-04-02"
    assert session.record.tier is RcaEvidenceTier.CUTOFF_CONSTRAINED
    assert session.record.role == "critique"
    assert session.record.effective_capabilities == (ModelCapability.TEXT_GENERATION,)
    assert session.record.opened_at == NOW_TEXT
    assert session.verified_cutoff == date(2024, 4, 1)
    entries = verifier.sessions()
    assert len(entries) == 1
    assert entries[0].sequence == 1
    assert entries[0].previous_hash == "0" * 64
    verifier.verify()


def test_session_chain_grows_and_detects_tampering() -> None:
    verifier = _verifier(_registry(), _attestations())
    _open(verifier)
    _open(verifier, role="research")
    entries = verifier.sessions()
    assert [entry.sequence for entry in entries] == [1, 2]
    assert entries[1].previous_hash == entries[0].entry_hash
    verify_session_chain(entries)
    assert entries[0].record.session_id != entries[1].record.session_id

    with pytest.raises(RegistryError):
        verify_session_chain((entries[1],))
    bad_hash = SessionLedgerEntry(
        sequence=1,
        previous_hash="0" * 64,
        entry_hash="f" * 64,
        opened_at=entries[0].opened_at,
        record=entries[0].record,
    )
    with pytest.raises(RegistryError):
        verify_session_chain((bad_hash,))
    tampered_record = replace(entries[0].record, actor="different-actor")
    with pytest.raises(RegistryError):
        verify_session_chain(
            (
                SessionLedgerEntry(
                    sequence=1,
                    previous_hash="0" * 64,
                    entry_hash=entries[0].entry_hash,
                    opened_at=entries[0].opened_at,
                    record=tampered_record,
                ),
            )
        )


def test_verification_is_deterministic_across_verifiers() -> None:
    registry = _registry()
    attestations = _attestations()
    first = _verifier(registry, attestations)
    second = _verifier(registry, attestations)
    _open(first)
    _open(second)
    assert first.sessions()[0].entry_hash == second.sessions()[0].entry_hash


def test_unknown_deployments_are_rejected() -> None:
    verifier = _verifier(_registry(), _attestations())
    with pytest.raises(SessionVerificationError) as error:
        _open(verifier, record_id="foundry/unknown")
    assert error.value.reasons == ("unknown-deployment",)
    assert verifier.sessions() == ()


def test_revoked_deployments_are_rejected() -> None:
    registry = _registry()
    registry.revoke(
        "foundry/gpt-4.1",
        actor="registry-operator",
        reason="weights changed",
        revoked_at=NOW,
    )
    verifier = _verifier(registry, _attestations())
    with pytest.raises(SessionVerificationError) as error:
        _open(verifier)
    assert error.value.reasons == ("revoked-deployment",)


def test_mutable_deployments_are_rejected_for_attested_tiers() -> None:
    verifier = _verifier(_registry(immutable_weights=False), _attestations())
    with pytest.raises(SessionVerificationError) as error:
        _open(verifier)
    assert error.value.reasons == ("mutable-deployment",)
    advisory = _open(verifier, tier=RcaEvidenceTier.ADVISORY)
    assert advisory.record.attestation_id is None
    assert advisory.record.verified_cutoff is None
    assert len(verifier.sessions()) == 1


def test_role_restrictions_block_sessions() -> None:
    restricted = _verifier(
        _registry(allowed_roles=frozenset({"research"})), _attestations()
    )
    with pytest.raises(SessionVerificationError) as error:
        _open(restricted, role="critique")
    assert error.value.reasons == ("role-not-permitted",)
    _open(restricted, role="research")
    assert len(restricted.sessions()) == 1


def test_missing_revoked_or_mismatched_attestations_are_rejected() -> None:
    verifier = _verifier(_registry(), AttestationRegistry())
    with pytest.raises(SessionVerificationError) as error:
        _open(verifier)
    assert error.value.reasons == ("unattested-deployment",)

    attestations = AttestationRegistry()
    attestations.register(_attestation())
    attestations.revoke("attest-2024-04")
    revoked = _verifier(_registry(), attestations)
    with pytest.raises(SessionVerificationError) as error:
        _open(revoked)
    assert error.value.reasons == ("revoked-attestation",)

    mismatched = AttestationRegistry()
    mismatched.register(_attestation(model_id="gpt-4o"))
    wrong_model = _verifier(_registry(), mismatched)
    with pytest.raises(SessionVerificationError) as error:
        _open(wrong_model)
    assert error.value.reasons == ("attestation-mismatch",)


def test_conservative_cutoff_must_precede_earliest_observation() -> None:
    verifier = _verifier(_registry(), _attestations())
    with pytest.raises(SessionVerificationError) as error:
        _open(verifier, earliest_observation=date(2024, 4, 1))
    assert error.value.reasons == ("cutoff-not-before-observation",)
    with pytest.raises(SessionVerificationError) as error:
        _open(verifier, earliest_observation=date(2024, 3, 31))
    assert error.value.reasons == ("cutoff-not-before-observation",)
    _open(verifier, earliest_observation=date(2024, 4, 2))
    assert len(verifier.sessions()) == 1


def test_denied_capabilities_are_rejected_whenever_they_appear() -> None:
    declaring = _verifier(
        _registry(
            capabilities=frozenset(
                {ModelCapability.TEXT_GENERATION, ModelCapability.WEB_BROWSING}
            )
        ),
        _attestations(),
    )
    with pytest.raises(SessionVerificationError) as error:
        _open(declaring)
    assert error.value.reasons == ("denied-capability-declared:web_browsing",)

    requesting = _verifier(_registry(), _attestations())
    with pytest.raises(SessionVerificationError) as error:
        _open(
            requesting,
            requested_capabilities=frozenset({ModelCapability.WORKSPACE_HISTORY}),
        )
    assert error.value.reasons == (
        "denied-capability-requested:workspace_history",
        "capability-not-declared:workspace_history",
    )
    with pytest.raises(SessionVerificationError) as error:
        _open(
            requesting,
            requested_capabilities=frozenset({ModelCapability.STRATEGY_EDITING}),
        )
    assert "denied-capability-requested:strategy_editing" in error.value.reasons


def test_capability_assertions_are_rechecked_at_call_time() -> None:
    registry = _registry()
    verifier = _verifier(registry, _attestations())
    session = _open(verifier)
    session_id = session.record.session_id
    verifier.assert_capability(session_id, ModelCapability.TEXT_GENERATION)
    with pytest.raises(SessionVerificationError) as error:
        verifier.assert_capability(session_id, ModelCapability.WEB_BROWSING)
    assert error.value.reasons == ("capability-not-granted:web_browsing",)
    with pytest.raises(SessionVerificationError) as error:
        verifier.assert_capability("missing", ModelCapability.TEXT_GENERATION)
    assert error.value.reasons == ("unknown-session",)
    registry.revoke(
        "foundry/gpt-4.1",
        actor="registry-operator",
        reason="weights changed",
        revoked_at=NOW,
    )
    with pytest.raises(SessionVerificationError) as error:
        verifier.assert_capability(session_id, ModelCapability.TEXT_GENERATION)
    assert error.value.reasons == ("deployment-revoked-after-open",)


def test_invalid_tier_workspace_and_actor_inputs_are_rejected() -> None:
    verifier = _verifier(_registry(), _attestations())
    with pytest.raises(TierPolicyError):
        _open(verifier, tier="cutoff_constrained")
    with pytest.raises(RegistryError):
        _open(verifier, workspace_scope="../escape")
    with pytest.raises(RegistryError):
        _open(verifier, actor="bad actor!")


def test_model_profile_composes_with_the_agent_router_identity() -> None:
    record = _record()
    attestation = _attestation()
    profile = model_profile_for(record, attestation)
    assert isinstance(profile, ModelProfile)
    assert profile.profile_id == "foundry/gpt-4.1"
    assert profile.version == "2024.4.9"
    assert profile.provider == "azure_ai_foundry"
    assert profile.model == "rca-critique-deployment"
    assert profile.knowledge_cutoff == "2024-04-01"
    assert profile.cutoff_evidence == attestation.content_sha256
    with pytest.raises(RegistryError):
        model_profile_for(record, _attestation(attestation_id="other"))
    with pytest.raises(RegistryError):
        model_profile_for(_record(model_version="snapshot-2024"), attestation)
