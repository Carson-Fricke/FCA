"""Filesystem-backed workspace catalog over the local workspace provider.

Local mode persists each workspace as a literal, Git-backed directory under an
operator-configured root (by default the operating system's local app-data
directory).  This store implements the workspace catalog the HTTP service
consumes: ``list`` scans the root, ``get`` opens one workspace, and ``add``
provisions a real directory from the workspace manifest template.  Workspaces
therefore survive process restarts; the manifest on disk is the source of
truth and the summary rows are derived from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from thesis.workspaces.local import (
    LocalWorkspaceProvider,
    WorkspaceNotFoundError,
    WorkspaceRepositoryError,
)
from thesis.workspaces.manifest import (
    WorkspaceManifest,
    dump_workspace_manifest,
)

_MANIFEST_RELATIVE = ".thesis/workspace.yaml"
_ACTIVITY_DIRECTORY = ".catalog"
_ACTIVITY_FILENAME = "activity.json"

ActivityEntry = dict[str, object]
ActivityDocument = dict[str, list[ActivityEntry]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _initials(name: str) -> str:
    words = [word for word in name.split() if word]
    if not words:
        return "WS"
    if len(words) == 1:
        return words[0][:2].upper()
    return "".join(word[0] for word in words[:2]).upper()


def summary_from_manifest(manifest: WorkspaceManifest) -> dict[str, object]:
    """Project one workspace manifest onto the catalog summary shape."""
    return {
        "id": manifest.workspace_id,
        "name": manifest.charter.title,
        "description": manifest.charter.objective,
        "initials": _initials(manifest.charter.title),
        "created_at": _iso_z(manifest.created_at),
    }


def manifest_for_summary(
    template: WorkspaceManifest, summary: dict[str, object]
) -> WorkspaceManifest:
    """Render a workspace manifest from the template and a catalog summary."""
    workspace_id = str(summary["id"])
    name = str(summary.get("name") or workspace_id)
    description = str(summary.get("description") or "").strip()
    values = template.model_dump(mode="json")
    values["workspace_id"] = workspace_id
    values["created_at"] = str(summary.get("created_at") or _iso_z(_utc_now()))
    values["charter"]["family_id"] = workspace_id
    values["charter"]["title"] = name
    if description:
        values["charter"]["objective"] = description
    values["repository_identity"]["repository_id"] = workspace_id
    return WorkspaceManifest.model_validate(values)


class LocalWorkspaceCatalogStore:
    """Catalog of literal on-disk workspaces under one provider root."""

    def __init__(
        self,
        provider: LocalWorkspaceProvider,
        template: WorkspaceManifest,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._template = template
        self._clock = clock if clock is not None else _utc_now

    @property
    def provider(self) -> LocalWorkspaceProvider:
        return self._provider

    def list(self) -> tuple[dict[str, object], ...]:
        """Return every persisted workspace summary, ordered by id."""
        summaries: list[dict[str, object]] = []
        for child in sorted(self._provider.root.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            if not (child / _MANIFEST_RELATIVE).is_file():
                continue
            if self.is_deleted(child.name):
                continue
            try:
                summaries.append(
                    summary_from_manifest(self._provider.open(child.name).manifest)
                )
            except (WorkspaceRepositoryError, ValueError):
                # Skip directories that are not valid Thesis workspaces.
                continue
        return tuple(summaries)

    def get(
        self, workspace_id: str, *, include_deleted: bool = False
    ) -> dict[str, object] | None:
        if self.is_deleted(workspace_id):
            if not include_deleted:
                return None
            return json.loads(
                self._deletion_path(workspace_id).read_text(encoding="utf-8")
            )["workspace"]
        try:
            workspace = self._provider.open(workspace_id)
        except WorkspaceNotFoundError:
            return None
        except (WorkspaceRepositoryError, ValueError):
            return None
        return summary_from_manifest(workspace.manifest)

    def add(self, summary: dict[str, object]) -> dict[str, object]:
        """Provision the workspace directory and return its summary."""
        if self.is_deleted(str(summary["id"])):
            raise ValueError("workspace has been deleted")
        manifest = manifest_for_summary(self._template, summary)
        workspace = self._provider.create(manifest)
        return summary_from_manifest(workspace.manifest)

    def _deletion_path(self, workspace_id: str) -> Path:
        digest = hashlib.sha256(workspace_id.encode()).hexdigest()
        return (
            self._provider.root / _ACTIVITY_DIRECTORY / "deletions" / f"{digest}.json"
        )

    def mark_deleted(self, workspace_id: str, deleted_at: str) -> None:
        """Durably mark all storage owned by this ID for later removal."""
        path = self._deletion_path(workspace_id)
        if path.exists():
            return
        workspace = self._provider.open(workspace_id)
        summary = {
            **summary_from_manifest(workspace.manifest),
            "owner_id": workspace.manifest.owner.owner_id,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        )
        try:
            with handle:
                json.dump(
                    {
                        "workspace_id": workspace_id,
                        "status": "deletion_pending",
                        "scope": "all_workspace_data",
                        "deleted_at": deleted_at,
                        "workspace": summary,
                    },
                    handle,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, path)
        finally:
            if os.path.exists(handle.name):
                os.unlink(handle.name)

    def is_deleted(self, workspace_id: str) -> bool:
        return self._deletion_path(workspace_id).exists()

    def deleted_workspace_ids(self) -> tuple[str, ...]:
        directory = self._provider.root / _ACTIVITY_DIRECTORY / "deletions"
        return tuple(
            str(json.loads(path.read_text(encoding="utf-8"))["workspace_id"])
            for path in sorted(directory.glob("*.json"))
        )

    def replace_manifest(self, workspace_id: str, manifest: WorkspaceManifest) -> None:
        """Atomically replace one workspace's manifest without touching content.

        The workspace is reopened through the provider so its identity and Git
        boundary are re-verified before the write, and the new document is
        staged in the manifest's own directory and moved into place with
        :func:`os.replace`, so a crash never leaves a partially written
        manifest.  Only ``.thesis/workspace.yaml`` changes; every other file in
        the workspace is left exactly as it was.
        """
        workspace = self._provider.open(workspace_id)
        if manifest.workspace_id != workspace_id:
            raise ValueError("manifest workspace_id must match the selected workspace")
        path = workspace.root / _MANIFEST_RELATIVE
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".workspace-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(dump_workspace_manifest(manifest))
            os.replace(handle.name, path)
        except BaseException:
            if os.path.exists(handle.name):
                os.unlink(handle.name)
            raise

    def add_activity(self, item: dict[str, object]) -> dict[str, object]:
        workspace_id = str(item["workspace_id"])
        activity = dict(self._read_activity())
        current = activity.get(workspace_id, [])
        current.insert(0, dict(item))
        activity[workspace_id] = current
        self._write_activity(activity)
        return dict(item)

    def activity(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(
            dict(entry) for entry in self._read_activity().get(workspace_id, ())
        )

    def _activity_path(self) -> Path:
        return self._provider.root / _ACTIVITY_DIRECTORY / _ACTIVITY_FILENAME

    def _read_activity(self) -> ActivityDocument:
        path = self._activity_path()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return {}
        if not isinstance(document, dict):
            return {}
        return {
            str(key): [dict(entry) for entry in value if isinstance(entry, dict)]
            for key, value in document.items()
            if isinstance(value, list)
        }

    def _write_activity(self, activity: ActivityDocument) -> None:
        path = self._activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".activity-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(activity, handle)
            os.replace(handle.name, path)
        except BaseException:
            if os.path.exists(handle.name):
                os.unlink(handle.name)
            raise


__all__ = [
    "LocalWorkspaceCatalogStore",
    "manifest_for_summary",
    "summary_from_manifest",
]
