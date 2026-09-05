from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from thesis.registry.models import ModelRegistry, RegistryConflictError
from thesis.registry.records import (
    DeploymentStatus,
    ModelCapability,
    ModelRegistryRecord,
    RegistryError,
    RoleRestrictionError,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOW_TEXT = "2026-09-01T12:00:00.000000Z"


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
        allowed_roles=frozenset({"critique"}),
        immutable_weights=True,
        attestation_id="attest-2024-04",
        registered_at=NOW_TEXT,
        registered_by="registry-operator",
    )
    if not overrides:
        return base
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_record_hashes_canonically_and_is_reproducible() -> None:
    record = _record()
    again = _record()
    assert record.content_sha256 == again.content_sha256
    document = record.canonical_document
    assert document["schema"] == "fca-thesis-model-registry-record-v1"
    assert document["recordId"] == "foundry/gpt-4.1"
    assert document["provider"] == "azure_ai_foundry"
    assert document["modelId"] == "gpt-4.1"
    assert document["modelVersion"] == "2024.4.9"
    assert document["deployment"] == "rca-critique-deployment"
    assert document["capabilities"] == ["text_generation"]
    assert document["allowedRoles"] == ["critique"]
    assert document["status"] == "active"
    assert document["attestationId"] == "attest-2024-04"


def test_record_rejects_invalid_identities_and_policies() -> None:
    with pytest.raises(RegistryError):
        _record(version=0)
    with pytest.raises(RegistryError):
        _record(version=True)
    with pytest.raises(RegistryError):
        _record(model_version="")
    with pytest.raises(RegistryError):
        _record(context_window_tokens=0)
    with pytest.raises(RegistryError):
        _record(max_output_tokens=0)
    with pytest.raises(RegistryError):
        _record(capabilities=frozenset())
    with pytest.raises(RegistryError):
        _record(capabilities=frozenset({"text_generation"}))
    with pytest.raises(RegistryError):
        _record(allowed_roles=frozenset())
    with pytest.raises(RegistryError):
        _record(allowed_roles=frozenset({"orchestrator"}))
    with pytest.raises(RegistryError):
        _record(immutable_weights="yes")
    with pytest.raises(RegistryError):
        _record(status=DeploymentStatus.REVOKED)
    with pytest.raises(RegistryError):
        _record(revoked_at=NOW_TEXT)
    with pytest.raises(RegistryError):
        _record(attestation_id="")
    with pytest.raises(RegistryError):
        _record(registered_at="2026-09-01T12:00:00")
    with pytest.raises(RegistryError):
        _record(registered_by="bad actor!")


def test_revoked_records_require_revocation_metadata() -> None:
    record = _record(
        status=DeploymentStatus.REVOKED,
        revoked_at=NOW_TEXT,
        revocation_reason="deployment weights changed",
    )
    assert record.status is DeploymentStatus.REVOKED
    assert record.canonical_document["revokedAt"] == NOW_TEXT
    assert record.canonical_document["revocationReason"] == (
        "deployment weights changed"
    )


def test_registry_register_is_idempotent_for_identical_content() -> None:
    registry = ModelRegistry()
    first = registry.register(_record())
    second = registry.register(_record())
    assert first is second
    assert len(registry.records()) == 1
    assert registry.history("foundry/gpt-4.1") == (first,)


def test_registry_register_requires_increasing_versions() -> None:
    registry = ModelRegistry()
    registry.register(_record(version=2))
    with pytest.raises(RegistryConflictError):
        registry.register(_record(version=1, model_version="2024.4.10"))
    registry.register(_record(version=3, model_version="2024.4.10"))
    assert [record.version for record in registry.history("foundry/gpt-4.1")] == [
        2,
        3,
    ]
    current = registry.get("foundry/gpt-4.1")
    assert current is not None
    assert current.model_version == "2024.4.10"


def test_registry_revoke_creates_new_version_and_is_final() -> None:
    registry = ModelRegistry()
    registry.register(_record())
    revoked = registry.revoke(
        "foundry/gpt-4.1",
        actor="registry-operator",
        reason="deployment weights changed",
        revoked_at=NOW,
    )
    assert revoked.version == 2
    assert revoked.status is DeploymentStatus.REVOKED
    assert revoked.revocation_reason == "deployment weights changed"
    assert registry.get("foundry/gpt-4.1") is revoked
    with pytest.raises(RegistryConflictError):
        registry.register(_record(version=3))
    with pytest.raises(RegistryConflictError):
        registry.revoke(
            "foundry/gpt-4.1",
            actor="registry-operator",
            reason="again",
            revoked_at=NOW,
        )


def test_registry_revoke_rejects_unknown_records_and_blank_reasons() -> None:
    registry = ModelRegistry()
    with pytest.raises(RegistryConflictError):
        registry.revoke(
            "missing",
            actor="registry-operator",
            reason="none",
            revoked_at=NOW,
        )
    registry.register(_record())
    with pytest.raises(RegistryError):
        registry.revoke(
            "foundry/gpt-4.1",
            actor="registry-operator",
            reason="   ",
            revoked_at=NOW,
        )


def test_role_restrictions_are_enforced() -> None:
    registry = ModelRegistry()
    registry.register(_record(allowed_roles=frozenset({"critique", "research"})))
    assert registry.check_role("foundry/gpt-4.1", "critique").record_id == (
        "foundry/gpt-4.1"
    )
    with pytest.raises(RoleRestrictionError):
        registry.check_role("foundry/gpt-4.1", "primary")
    with pytest.raises(RoleRestrictionError):
        registry.check_role("foundry/gpt-4.1", "orchestrator")
    with pytest.raises(RoleRestrictionError):
        registry.check_role("missing", "critique")


def test_role_restrictions_block_revoked_records() -> None:
    registry = ModelRegistry()
    registry.register(_record())
    registry.revoke(
        "foundry/gpt-4.1",
        actor="registry-operator",
        reason="weights changed",
        revoked_at=NOW,
    )
    with pytest.raises(RoleRestrictionError):
        registry.check_role("foundry/gpt-4.1", "critique")


def test_registry_lists_current_versions_sorted_by_id() -> None:
    registry = ModelRegistry()
    registry.register(_record(record_id="b-model"))
    registry.register(_record(record_id="a-model"))
    assert [record.record_id for record in registry.records()] == [
        "a-model",
        "b-model",
    ]
