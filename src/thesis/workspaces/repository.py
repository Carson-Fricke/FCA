"""Structural boundary for local and remotely backed workspace repositories."""

from pathlib import Path
from typing import Protocol

from thesis.workspaces.local import LocalWorkspace
from thesis.workspaces.manifest import WorkspaceManifest


class WorkspaceRepositoryProvider(Protocol):
    """A repository provider exposes only verified, confined local checkouts."""

    @property
    def root(self) -> Path: ...

    def create(self, manifest: WorkspaceManifest) -> LocalWorkspace: ...

    def open(self, workspace_id: str) -> LocalWorkspace: ...

    def resolve_path(
        self,
        workspace_id: str,
        relative: str | Path = ".",
        *,
        must_exist: bool = False,
    ) -> Path: ...

    def publish(self, workspace_id: str) -> str: ...

    def repository_uri(self, workspace_id: str) -> str: ...
