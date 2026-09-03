"""Azure persistence contract for independently hosted workspace entities."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

from pydantic import BeforeValidator, StringConstraints, model_validator

from thesis.workspaces.manifest import (
    Identifier,
    Sha256Digest,
    StrictWorkspaceModel,
    WorkspaceId,
    WorkspaceManifest,
    manifest_sha256,
)


def _utc_datetime(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a UTC offset")
        return value.astimezone(UTC)
    return value


AzureResourceId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=2_048,
        pattern=r"^/subscriptions/[A-Za-z0-9-]+/.+$",
    ),
]


class AzureWorkspaceEntity(StrictWorkspaceModel):
    """Durable locator for one Azure workspace's isolated resources."""

    schema_version: Literal[1]
    workspace_id: WorkspaceId
    owner_id: Identifier
    created_by: Identifier
    repository_id: Identifier
    git_repository_uri: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_048)
    ]
    execution_scope_id: AzureResourceId
    persistence_scope_id: AzureResourceId
    manifest_sha256: Sha256Digest
    created_at: Annotated[datetime, BeforeValidator(_utc_datetime)]

    @model_validator(mode="after")
    def _repository_uri_is_non_secret_https(self) -> AzureWorkspaceEntity:
        parsed = urlsplit(self.git_repository_uri)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "git_repository_uri must be an HTTPS locator without credentials, "
                "query, or fragment"
            )
        return self

    @classmethod
    def from_manifest(
        cls,
        manifest: WorkspaceManifest,
        *,
        git_repository_uri: str,
        execution_scope_id: str,
        persistence_scope_id: str,
    ) -> AzureWorkspaceEntity:
        """Bind an Azure manifest to provider-assigned resource identities."""
        if manifest.repository_identity.provider != "azure":
            raise ValueError("Azure entity requires an Azure repository identity")
        return cls(
            schema_version=1,
            workspace_id=manifest.workspace_id,
            owner_id=manifest.owner.owner_id,
            created_by=manifest.created_by,
            repository_id=manifest.repository_identity.repository_id,
            git_repository_uri=git_repository_uri,
            execution_scope_id=execution_scope_id,
            persistence_scope_id=persistence_scope_id,
            manifest_sha256=manifest_sha256(manifest),
            created_at=manifest.created_at,
        )

    @property
    def partition_key(self) -> str:
        return f"workspaces|{quote(self.owner_id, safe='._-~')}"

    @property
    def row_key(self) -> str:
        return quote(self.workspace_id, safe="._-~")


def azure_workspace_to_entity(workspace: AzureWorkspaceEntity) -> dict[str, object]:
    """Map a workspace locator to Azure Table-compatible primitive fields."""
    return {
        "PartitionKey": workspace.partition_key,
        "RowKey": workspace.row_key,
        "SchemaVersion": workspace.schema_version,
        "WorkspaceId": workspace.workspace_id,
        "OwnerId": workspace.owner_id,
        "CreatedBy": workspace.created_by,
        "RepositoryId": workspace.repository_id,
        "GitRepositoryUri": workspace.git_repository_uri,
        "ExecutionScopeId": workspace.execution_scope_id,
        "PersistenceScopeId": workspace.persistence_scope_id,
        "ManifestSha256": workspace.manifest_sha256,
        "CreatedAt": workspace.created_at,
    }


def azure_workspace_from_entity(
    entity: Mapping[str, object],
) -> AzureWorkspaceEntity:
    """Validate an Azure Table entity and its owner/workspace key binding."""
    created_at = entity["CreatedAt"]
    if not isinstance(created_at, datetime):
        raise TypeError("Azure workspace CreatedAt must be a datetime")
    workspace = AzureWorkspaceEntity.model_validate(
        {
            "schema_version": entity["SchemaVersion"],
            "workspace_id": entity["WorkspaceId"],
            "owner_id": entity["OwnerId"],
            "created_by": entity["CreatedBy"],
            "repository_id": entity["RepositoryId"],
            "git_repository_uri": entity["GitRepositoryUri"],
            "execution_scope_id": entity["ExecutionScopeId"],
            "persistence_scope_id": entity["PersistenceScopeId"],
            "manifest_sha256": entity["ManifestSha256"],
            "created_at": created_at,
        }
    )
    if entity.get("PartitionKey") != workspace.partition_key:
        raise ValueError("Azure workspace PartitionKey does not match owner identity")
    if entity.get("RowKey") != workspace.row_key:
        raise ValueError("Azure workspace RowKey does not match workspace identity")
    return workspace


__all__ = [
    "AzureResourceId",
    "AzureWorkspaceEntity",
    "azure_workspace_from_entity",
    "azure_workspace_to_entity",
]
