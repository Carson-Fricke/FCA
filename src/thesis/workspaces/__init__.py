"""Workspace identity, repository lifecycle, and provider contracts."""

from thesis.workspaces.azure_contract import (
    AzureWorkspaceEntity,
    azure_workspace_from_entity,
    azure_workspace_to_entity,
)
from thesis.workspaces.local import (
    LocalWorkspace,
    LocalWorkspaceProvider,
    WorkspaceAlreadyExistsError,
    WorkspaceNotFoundError,
    WorkspacePathError,
    WorkspaceRepositoryError,
)
from thesis.workspaces.manifest import (
    PlatformBundleReference,
    RepositoryIdentity,
    RuntimeProfileReference,
    StrategyFamilyCharter,
    WorkspaceLimits,
    WorkspaceManifest,
    WorkspaceManifestError,
    WorkspaceOwner,
    WorkspaceSecretError,
    dump_workspace_manifest,
    load_workspace_manifest,
    manifest_sha256,
    write_workspace_manifest,
)

__all__ = [
    "AzureWorkspaceEntity",
    "LocalWorkspace",
    "LocalWorkspaceProvider",
    "PlatformBundleReference",
    "RepositoryIdentity",
    "RuntimeProfileReference",
    "StrategyFamilyCharter",
    "WorkspaceAlreadyExistsError",
    "WorkspaceLimits",
    "WorkspaceManifest",
    "WorkspaceManifestError",
    "WorkspaceNotFoundError",
    "WorkspaceOwner",
    "WorkspacePathError",
    "WorkspaceRepositoryError",
    "WorkspaceSecretError",
    "azure_workspace_from_entity",
    "azure_workspace_to_entity",
    "dump_workspace_manifest",
    "load_workspace_manifest",
    "manifest_sha256",
    "write_workspace_manifest",
]
