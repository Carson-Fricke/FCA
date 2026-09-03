from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from thesis.workspaces import (
    AzureWorkspaceEntity,
    azure_workspace_from_entity,
    azure_workspace_to_entity,
)
from thesis.workspaces.manifest import WorkspaceManifest

ROOT = Path(__file__).parents[3]
EXAMPLE = ROOT / "config" / "thesis" / "workspace.yaml"


def _azure_manifest() -> WorkspaceManifest:
    values = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    values["runtime_profile"] = {
        "profile_id": "azure-managed",
        "version": "1.0.0",
        "mode": "azure",
    }
    values["repository_identity"]["provider"] = "azure"
    return WorkspaceManifest.model_validate(values)


def _entity() -> AzureWorkspaceEntity:
    return AzureWorkspaceEntity.from_manifest(
        _azure_manifest(),
        git_repository_uri=(
            "https://dev.azure.com/example/thesis/_git/equity-momentum"
        ),
        execution_scope_id=(
            "/subscriptions/00000000-0000-0000-0000-000000000000/"
            "resourceGroups/thesis-equity-momentum-execution"
        ),
        persistence_scope_id=(
            "/subscriptions/00000000-0000-0000-0000-000000000000/"
            "resourceGroups/thesis-equity-momentum-state"
        ),
    )


def test_azure_workspace_entity_round_trips_with_bound_table_keys() -> None:
    workspace = _entity()
    entity = azure_workspace_to_entity(workspace)

    assert entity["PartitionKey"] == "workspaces|local-operator"
    assert entity["RowKey"] == "equity-momentum"
    assert entity["CreatedBy"] == "local-operator"
    assert entity["ManifestSha256"] == _azure_manifest().content_hash
    assert azure_workspace_from_entity(entity) == workspace


def test_azure_workspace_entity_rejects_cross_workspace_key_substitution() -> None:
    entity = azure_workspace_to_entity(_entity())
    entity["RowKey"] = "another-workspace"

    with pytest.raises(ValueError, match="RowKey"):
        azure_workspace_from_entity(entity)


@pytest.mark.parametrize(
    "uri",
    [
        "https://user:secret@dev.azure.com/example/repository",
        "https://dev.azure.com/example/repository?token=secret",
        "ssh://git@ssh.dev.azure.com/example/repository",
    ],
)
def test_azure_workspace_entity_rejects_secret_bearing_repository_uris(
    uri: str,
) -> None:
    values = _entity().model_dump()
    values["git_repository_uri"] = uri

    with pytest.raises(ValidationError, match="without credentials"):
        AzureWorkspaceEntity.model_validate(values)
