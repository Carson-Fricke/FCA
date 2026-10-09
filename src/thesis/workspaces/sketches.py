"""Committed evolving strategy sketches and immutable research handoff references."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from thesis.persistence.git import GitCoordinator, GitMaterializationError
from thesis.workspaces.repository import WorkspaceGit, WorkspaceRepositoryProvider
from thesis.workspaces.sketch_contract import validate_sketch


class SketchPublicationError(RuntimeError):
    """A local mutation exists; reconcile this reference rather than retrying it."""

    def __init__(self, reference: Mapping[str, object]) -> None:
        super().__init__("Sketch committed locally; workspace publication incomplete")
        self.reference = dict(reference)


def _bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


class WorkspaceSketches:
    """Store immutable revisions and a CAS-fenced current pointer in workspace Git."""

    def __init__(
        self,
        repositories: WorkspaceRepositoryProvider,
        *,
        git_factory: Callable[[Path], WorkspaceGit] = GitCoordinator,
    ) -> None:
        self.repositories = repositories
        self._git_factory = git_factory

    @staticmethod
    def _path(sketch_id: str) -> str:
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", sketch_id) is None or sketch_id in {
            "con",
            "prn",
            "aux",
            "nul",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
        }:
            raise ValueError("sketch_id must be a safe identifier")
        return f"sketches/{sketch_id}"

    def save(
        self,
        workspace_id: str,
        sketch_id: str,
        document: Mapping[str, object],
        *,
        expected_version: str | None = None,
    ) -> dict[str, object]:
        relative = self._path(sketch_id)
        sketch = validate_sketch(document)
        payload = _bytes(sketch)
        version = hashlib.sha256(payload).hexdigest()
        root = self.repositories.open(workspace_id).root
        git = self._git_factory(root)
        with git.publication_lock():
            head = git.head()
            current_path = f"{relative}/current.json"
            exists = current_path in git.tracked_paths(relative)
            if expected_version is None and exists:
                raise ValueError(
                    "sketch already exists; update with its current version"
                )
            if expected_version is not None:
                if not exists or head is None:
                    raise KeyError(sketch_id)
                current = json.loads(git.read_blobs(head, [current_path])[current_path])
                if current["version"] != expected_version:
                    raise ValueError("stale sketch version; read before updating")
                if version == expected_version:
                    raise ValueError("update must change the sketch")
            files = {
                f"{relative}/revisions/{version}.json": payload,
                current_path: _bytes({"version": version}),
            }
            destinations = {}
            for path in files:
                destination = self.repositories.resolve_path(workspace_id, path)
                current_dir = root
                for part in path.split("/"):
                    if current_dir.is_dir() and any(
                        child.name.casefold() == part.casefold() and child.name != part
                        for child in current_dir.iterdir()
                    ):
                        raise ValueError("sketch path has a case collision")
                    current_dir = current_dir / part
                    if current_dir.is_symlink():
                        raise ValueError("sketch paths cannot contain symlinks")
                if destination.exists() and (
                    head is None
                    or not git.file_matches(head, path, destination.read_bytes())
                ):
                    raise ValueError(
                        "sketch has uncommitted changes; refusing to overwrite"
                    )
                destinations[path] = destination
            if git.has_staged_changes(tuple(files)):
                raise ValueError("sketch has staged changes; refusing to overwrite")
            try:
                commit = git.commit_exact_files(
                    head, files, destinations, "revise strategy sketch " + sketch_id
                )
            except GitMaterializationError as error:
                raise SketchPublicationError(
                    self._reference(sketch_id, version, error.commit_hash)
                ) from error
        reference = self._reference(sketch_id, version, commit)
        try:
            self.repositories.publish(workspace_id)
        except Exception as error:
            raise SketchPublicationError(reference) from error
        return {"reference": reference, "sketch": sketch}

    @staticmethod
    def _reference(sketch_id: str, version: str, commit: str) -> dict[str, object]:
        return {"sketchId": sketch_id, "version": version, "gitCommit": commit}

    def read(
        self,
        workspace_id: str,
        sketch_id: str,
        *,
        git_commit: str | None = None,
        version: str | None = None,
    ) -> dict[str, object]:
        relative = self._path(sketch_id)
        git = self._git_factory(self.repositories.open(workspace_id).root)
        commit = git_commit or git.head()
        if commit is None or not git.is_ancestor(commit):
            raise ValueError("sketch commit is not reachable in this workspace")
        if version is None:
            current_path = f"{relative}/current.json"
            current = json.loads(git.read_blobs(commit, [current_path])[current_path])
            version = current["version"]
        if re.fullmatch(r"[0-9a-f]{64}", version) is None:
            raise ValueError("sketch version must be a content digest")
        revision_path = f"{relative}/revisions/{version}.json"
        payload = git.read_blobs(commit, [revision_path])[revision_path]
        if hashlib.sha256(payload).hexdigest() != version:
            raise ValueError("sketch revision does not match its content identity")
        sketch = validate_sketch(json.loads(payload))
        return {
            "reference": self._reference(sketch_id, version, commit),
            "sketch": sketch,
        }

    def verify(
        self, workspace_id: str, reference: Mapping[str, object]
    ) -> dict[str, object]:
        if set(reference) != {"sketchId", "version", "gitCommit"} or not all(
            isinstance(value, str) for value in reference.values()
        ):
            raise ValueError(
                "sketch reference must contain sketchId, version and gitCommit"
            )
        result = self.read(
            workspace_id,
            cast(str, reference["sketchId"]),
            git_commit=cast(str, reference["gitCommit"]),
            version=cast(str, reference["version"]),
        )
        return cast(dict[str, object], result["sketch"])

    def list(
        self, workspace_id: str, *, offset: int = 0, limit: int = 50
    ) -> dict[str, object]:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 200
        ):
            raise ValueError("offset must be nonnegative; limit must be from 1 to 200")
        git = self._git_factory(self.repositories.open(workspace_id).root)
        ids = sorted(
            path.split("/")[1]
            for path in git.tracked_paths("sketches")
            if path.endswith("/current.json")
        )
        items = [
            self.read(workspace_id, sketch_id)
            for sketch_id in ids[offset : offset + limit]
        ]
        return {
            "items": items,
            "total": len(ids),
            "nextOffset": offset + limit if offset + limit < len(ids) else None,
        }

    def history(self, workspace_id: str, sketch_id: str) -> Sequence[dict[str, object]]:
        """Expose committed revisions; each remains readable by its exact reference."""
        relative = self._path(sketch_id)
        current_path = f"{relative}/current.json"
        git = self._git_factory(self.repositories.open(workspace_id).root)
        result = []
        for commit, timestamp in git.path_revisions(relative):
            pointer = json.loads(git.read_blobs(commit, [current_path])[current_path])
            result.append(
                {"label": pointer["version"], "created_at": timestamp, "note": commit}
            )
        return result
