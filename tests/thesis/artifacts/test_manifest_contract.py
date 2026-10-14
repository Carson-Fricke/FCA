"""Pure manifest schema decoding rejects obsolete field spellings and omissions."""

from typing import cast

import pytest

from thesis.artifacts.importer import (
    ArtifactImportError,
    HarnessArtifact,
    HarnessManifest,
)


def manifest() -> HarnessManifest:
    return HarnessManifest(
        "manifest", (HarnessArtifact("metrics.json", "metrics.json", "a" * 64, 12),)
    )


def test_current_manifest_round_trips() -> None:
    current = manifest()
    assert HarnessManifest.from_mapping(current.canonical_document) == current


@pytest.mark.parametrize(
    "field", ["manifestId", "strategyVersion", "experimentId", "workflowExecutionId"]
)
def test_obsolete_manifest_field_spellings_are_rejected(field: str) -> None:
    payload = manifest().canonical_document
    payload["manifest_id" if field == "manifestId" else field.lower()] = payload.pop(
        field
    )
    with pytest.raises(ArtifactImportError, match="outdated"):
        HarnessManifest.from_mapping(payload)


def test_artifact_path_alias_is_rejected() -> None:
    payload = manifest().canonical_document
    artifact = cast(list[dict[str, object]], payload["artifacts"])[0]
    artifact["path"] = artifact.pop("uri")
    with pytest.raises(ArtifactImportError, match="invalid harness manifest artifact"):
        HarnessManifest.from_mapping(payload)
