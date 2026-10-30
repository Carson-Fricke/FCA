"""Workspace identity, repository lifecycle, and provider contracts."""

from thesis.workspaces.azure_contract import (
    AzureWorkspaceEntity,
    azure_workspace_from_entity,
    azure_workspace_to_entity,
)
from thesis.workspaces.catalog import (
    LocalWorkspaceCatalogStore,
    manifest_for_summary,
    summary_from_manifest,
)
from thesis.workspaces.gitea import GiteaWorkspaceProvider
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
from thesis.workspaces.program_contracts import (
    ProgramArtifactError,
    ProgramPublicationError,
)
from thesis.workspaces.programs import WorkspacePrograms
from thesis.workspaces.repository import WorkspaceRepositoryProvider

__all__ = [
    "GiteaWorkspaceProvider",
    "WorkspaceRepositoryProvider",
    "ProgramArtifactError",
    "ProgramPublicationError",
    "WorkspacePrograms",
    "AzureWorkspaceEntity",
    "LocalWorkspace",
    "LocalWorkspaceCatalogStore",
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
    "manifest_for_summary",
    "summary_from_manifest",
    "write_workspace_manifest",
]
