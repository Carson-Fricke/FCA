"""Gitless workspace and in-memory Git doubles for program artifact tests.

Real Git is an external process boundary: tests whose subject is program
artifact logic run against these deterministic doubles, while a small
``pytest.mark.slow`` suite keeps proving the real Git/filesystem semantics.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from thesis.persistence.git import GitMaterializationError, GitPublicationError
from thesis.workspaces.local import LocalWorkspace
from thesis.workspaces.manifest import WorkspaceManifest


class Repositories:
    """Gitless provider double: real directories, no Git subprocesses."""

    def __init__(self, root: Path, git_factory: MemoryGitFactory) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.git_factory = git_factory
        self.published: list[str] = []

    def create(self, manifest: WorkspaceManifest) -> LocalWorkspace:
        path = self.root / manifest.workspace_id
        path.mkdir(parents=True, exist_ok=True)
        return self.open(manifest.workspace_id)

    def open(self, workspace_id: str) -> LocalWorkspace:
        return LocalWorkspace(
            root=self._workspace_path(workspace_id),
            manifest=cast(WorkspaceManifest, None),
        )

    def store(self, workspace_id: str) -> MemoryGitStore:
        return self.git_factory.store(self._workspace_path(workspace_id))

    def resolve_path(
        self,
        workspace_id: str,
        relative: str | Path = ".",
        *,
        must_exist: bool = False,
    ) -> Path:
        return self.open(workspace_id).resolve_path(relative, must_exist=must_exist)

    def publish(self, workspace_id: str) -> str:
        self.published.append(workspace_id)
        return "local"

    def repository_uri(self, workspace_id: str) -> str:
        return self.open(workspace_id).root.as_uri()

    def _workspace_path(self, workspace_id: str) -> Path:
        path = self.root / workspace_id
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()


@dataclass(slots=True)
class _Commit:
    parent: str | None
    files: dict[str, bytes]
    committed_at: str


class MemoryGitStore:
    """Deterministic commit graph shared by every MemoryGit of one workspace."""

    def __init__(self) -> None:
        self.commits: dict[str, _Commit] = {}
        self.head: str | None = None
        self._counter = 0

    def commit(self, parent: str | None, files: Mapping[str, bytes]) -> str:
        self._counter += 1
        commit = f"{self._counter:040x}"
        snapshot = dict(self.commits[parent].files) if parent else {}
        snapshot.update(files)
        self.commits[commit] = _Commit(
            parent=parent,
            files=snapshot,
            committed_at=f"2026-01-01T00:00:{self._counter:02d}+00:00",
        )
        self.head = commit
        return commit

    def ancestors(self, commit: str | None) -> tuple[str, ...]:
        history: list[str] = []
        while commit is not None:
            history.append(commit)
            commit = self.commits[commit].parent
        return tuple(history)


class MemoryGit:
    """In-memory implementation of the ProgramGit storage port."""

    def __init__(self, repository: Path, store: MemoryGitStore) -> None:
        self.repository = repository.resolve()
        self._store = store

    def head(self) -> str | None:
        return self._store.head

    @contextlib.contextmanager
    def publication_lock(self) -> Iterator[None]:
        yield

    def tracked_paths(self, prefix: str) -> tuple[str, ...]:
        if self._store.head is None:
            return ()
        files = self._store.commits[self._store.head].files
        bound = prefix.rstrip("/") + "/"
        return tuple(sorted(path for path in files if path.startswith(bound)))

    def file_matches(self, commit_hash: str, path: str, payload: bytes) -> bool:
        entry = self._store.commits.get(commit_hash)
        return entry is not None and entry.files.get(path) == payload

    def is_ancestor(self, commit_hash: str) -> bool:
        return commit_hash in self._store.ancestors(self._store.head)

    def read_blobs(self, commit_hash: str, paths: Sequence[str]) -> dict[str, bytes]:
        entry = self._store.commits.get(commit_hash)
        blobs: dict[str, bytes] = {}
        for path in paths:
            if entry is None or path not in entry.files:
                raise GitPublicationError(
                    f"tracked artifact is not a regular file: {path}"
                )
            blobs[path] = entry.files[path]
        return blobs

    def read_file(self, commit_hash: str, path: str) -> bytes:
        return self.read_blobs(commit_hash, [path])[path]

    def path_revisions(self, prefix: str) -> tuple[tuple[str, str], ...]:
        bound = prefix.rstrip("/") + "/"
        revisions: list[tuple[str, str]] = []
        for commit in self._store.ancestors(self._store.head):
            entry = self._store.commits[commit]
            parent = self._store.commits[entry.parent].files if entry.parent else {}
            if any(
                path.startswith(bound) and parent.get(path) != payload
                for path, payload in entry.files.items()
            ):
                revisions.append((commit, entry.committed_at))
        return tuple(revisions)

    def path_revisions_many(
        self, prefixes: Sequence[str]
    ) -> dict[str, tuple[tuple[str, str], ...]]:
        return {prefix: self.path_revisions(prefix) for prefix in prefixes}

    def has_staged_changes(self, paths: Sequence[str]) -> bool:
        return False

    def commit_exact_files(
        self,
        head: str | None,
        files: Mapping[str, bytes],
        destinations: Mapping[str, Path],
        message: str,
    ) -> str:
        commit = self._store.commit(head, files)
        try:
            for relative, destination in destinations.items():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(files[relative])
        except OSError as error:
            raise GitMaterializationError(commit) from error
        return commit


class MemoryGitFactory:
    """Callable ProgramGit factory with one commit store per repository."""

    def __init__(self) -> None:
        self._stores: dict[Path, MemoryGitStore] = {}

    def store(self, repository: Path) -> MemoryGitStore:
        return self._stores.setdefault(repository.resolve(), MemoryGitStore())

    def __call__(self, repository: Path) -> MemoryGit:
        return MemoryGit(repository, self.store(repository))
