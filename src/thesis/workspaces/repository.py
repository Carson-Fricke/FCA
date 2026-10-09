"""Structural boundary for local and remotely backed workspace repositories."""

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
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


class WorkspaceGit(Protocol):
    """The Git surface workspace artifact operations require, as an injected storage port."""

    @property
    def repository(self) -> Path: ...

    def head(self) -> str | None: ...

    def publication_lock(self) -> AbstractContextManager[None]: ...

    def tracked_paths(self, prefix: str) -> tuple[str, ...]: ...

    def file_matches(self, commit_hash: str, path: str, payload: bytes) -> bool: ...

    def is_ancestor(self, commit_hash: str) -> bool: ...

    def read_blobs(
        self, commit_hash: str, paths: Sequence[str]
    ) -> dict[str, bytes]: ...

    def read_file(self, commit_hash: str, path: str) -> bytes: ...

    def path_revisions(self, prefix: str) -> tuple[tuple[str, str], ...]: ...

    def path_revisions_many(
        self, prefixes: Sequence[str]
    ) -> dict[str, tuple[tuple[str, str], ...]]: ...

    def has_staged_changes(self, paths: Sequence[str]) -> bool: ...

    def commit_exact_files(
        self,
        head: str | None,
        files: Mapping[str, bytes],
        destinations: Mapping[str, Path],
        message: str,
    ) -> str: ...
