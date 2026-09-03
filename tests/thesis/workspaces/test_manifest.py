from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from thesis.workspaces import (
    WorkspaceSecretError,
    dump_workspace_manifest,
    load_workspace_manifest,
    manifest_sha256,
)

ROOT = Path(__file__).parents[3]
EXAMPLE = ROOT / "config" / "thesis" / "workspace.yaml"


def test_example_records_the_complete_workspace_boundary() -> None:
    manifest = load_workspace_manifest(EXAMPLE)

    assert manifest.workspace_id == "equity-momentum"
    assert manifest.owner.owner_id == "local-operator"
    assert manifest.charter.markets == ("us-equities",)
    assert manifest.limits.max_repository_bytes == 10_737_418_240
    assert manifest.runtime_profile.profile_id == "local-loopback"
    assert manifest.platform_bundle.version == "1.0.0"
    assert manifest.repository_identity.repository_id == "equity-momentum"
    assert manifest.secret_references == {"broker_api_key": "${THESIS_BROKER_API_KEY}"}


def test_manifest_is_strict_immutable_and_rejects_unknown_fields() -> None:
    manifest = load_workspace_manifest(EXAMPLE)
    values = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    values["unknown"] = True

    with pytest.raises(ValidationError) as caught:
        type(manifest).model_validate(values)
    assert any(error["loc"] == ("unknown",) for error in caught.value.errors())

    field = "workspace_id"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(manifest, field, "other-workspace")


def test_manifest_rejects_raw_secrets_with_precise_path(tmp_path: Path) -> None:
    values = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    values["secret_references"]["broker_api_key"] = "raw-secret"
    path = tmp_path / "workspace.yaml"
    path.write_text(json.dumps(values), encoding="utf-8")

    with pytest.raises(
        WorkspaceSecretError,
        match="secret_references must contain only secret-store references",
    ):
        load_workspace_manifest(path)

    values = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    values["provider"] = {"api_key": "raw-secret"}
    path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(WorkspaceSecretError, match=r"provider\.api_key"):
        load_workspace_manifest(path)


def test_manifest_hash_and_serialization_are_location_independent(
    tmp_path: Path,
) -> None:
    manifest = load_workspace_manifest(EXAMPLE)
    first = tmp_path / "first.yaml"
    second = tmp_path / "nested" / "second.yaml"
    second.parent.mkdir()
    first.write_text(dump_workspace_manifest(manifest), encoding="utf-8")
    second.write_text(dump_workspace_manifest(manifest), encoding="utf-8")

    assert manifest_sha256(load_workspace_manifest(first)) == manifest.content_hash
    assert manifest_sha256(load_workspace_manifest(second)) == manifest.content_hash


def test_serializer_revalidates_model_copies_before_writing() -> None:
    manifest = load_workspace_manifest(EXAMPLE)
    unsafe = manifest.model_copy(
        update={"secret_references": {"broker_api_key": "raw-secret"}}
    )

    with pytest.raises(WorkspaceSecretError, match="secret_references"):
        dump_workspace_manifest(unsafe)


def test_manifest_rejects_incoherent_limits_and_provider_profile() -> None:
    values = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    values["limits"]["max_artifact_bytes"] = (
        values["limits"]["max_repository_bytes"] + 1
    )
    with pytest.raises(ValidationError, match="max_artifact_bytes"):
        load_workspace_manifest_from_values(values)

    values = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    values["repository_identity"]["provider"] = "azure"
    with pytest.raises(ValidationError, match="must match"):
        load_workspace_manifest_from_values(values)


def load_workspace_manifest_from_values(values: dict[str, object]):
    from thesis.workspaces.manifest import WorkspaceManifest

    return WorkspaceManifest.model_validate(values)
