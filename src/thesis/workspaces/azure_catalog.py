"""Azure workspace catalog with injected, remotely backed Git repositories.

Blob containers retain catalog metadata and operational scopes. With a
repository provider, Git owns the manifest and only fully provisioned
repositories are exposed. Blob-only composition remains available when no
Gitea configuration is supplied, including existing deployed catalog consumers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient

from thesis.workspaces.catalog import summary_from_manifest
from thesis.workspaces.local import WorkspaceNotFoundError
from thesis.workspaces.manifest import (
    WorkspaceManifest,
    dump_workspace_manifest,
    parse_workspace_manifest,
)
from thesis.workspaces.repository import WorkspaceRepositoryProvider

MANIFEST_PATH = "repository/.thesis/workspace.yaml"
REPOSITORY_PATH = "catalog/repository.json"
PROVISION_PATH = "catalog/provision.json"
ACTIVITY_PATH = "repository/.catalog/activity.json"
DELETION_PATH = "catalog/deletion.json"
DEFAULT_CONTAINER_PREFIX = "thesis-ws-"

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


def workspace_container_name(
    workspace_id: str, prefix: str = DEFAULT_CONTAINER_PREFIX
) -> str:
    """Derive the provisioned blob container name for one workspace."""
    return f"{prefix}{workspace_id.replace('_', '-').lower()}"


def azure_manifest_for_summary(
    template: WorkspaceManifest, summary: Mapping[str, object]
) -> WorkspaceManifest:
    """Render an Azure-mode workspace manifest from the template and a summary."""
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
    values["repository_identity"]["provider"] = "azure"
    values["repository_identity"]["repository_id"] = workspace_id
    values["runtime_profile"]["mode"] = "azure"
    return WorkspaceManifest.model_validate(values)


class AzureWorkspaceCatalogStore:
    """Catalog of blob-container workspaces under one container prefix."""

    def __init__(
        self,
        blob_service: BlobServiceClient,
        template: WorkspaceManifest,
        *,
        container_prefix: str = DEFAULT_CONTAINER_PREFIX,
        clock: Callable[[], datetime] | None = None,
        repositories: WorkspaceRepositoryProvider | None = None,
    ) -> None:
        if not container_prefix.strip():
            raise ValueError("container_prefix must be a non-empty string")
        self._blob_service = blob_service
        self._template = template
        self._prefix = container_prefix
        self._clock = clock if clock is not None else _utc_now
        self._containers: dict[str, ContainerClient] = {}
        self._repositories = repositories

    @property
    def provider(self) -> WorkspaceRepositoryProvider | None:
        """Expose the same confined checkout boundary as the local catalog."""
        return self._repositories

    @property
    def container_prefix(self) -> str:
        return self._prefix

    def _container(self, workspace_id: str) -> ContainerClient:
        name = workspace_container_name(workspace_id, self._prefix)
        client = self._containers.get(name)
        if client is None:
            client = self._blob_service.get_container_client(name)
            self._containers[name] = client
        return client

    def _read_manifest(self, container: ContainerClient) -> WorkspaceManifest | None:
        try:
            content = container.download_blob(MANIFEST_PATH).readall()
        except ResourceNotFoundError:
            return None
        try:
            return parse_workspace_manifest(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _synthetic_summary(self, workspace_id: str) -> dict[str, object]:
        return {
            "id": workspace_id,
            "name": workspace_id,
            "description": "",
            "initials": _initials(workspace_id),
            "created_at": _iso_z(self._clock()),
        }

    def list(self) -> tuple[dict[str, object], ...]:
        """Return every workspace container under the prefix, ordered by id."""
        summaries: list[dict[str, object]] = []
        for properties in self._blob_service.list_containers(
            name_starts_with=self._prefix
        ):
            name = getattr(properties, "name", None)
            if not isinstance(name, str) or not name.startswith(self._prefix):
                continue
            workspace_id = name[len(self._prefix) :]
            if not workspace_id:
                continue
            if self.is_deleted(workspace_id):
                continue
            manifest = self._read_manifest(
                self._blob_service.get_container_client(name)
            )
            if self._repositories is not None:
                try:
                    manifest = self._repositories.open(workspace_id).manifest
                except WorkspaceNotFoundError:
                    # An interrupted provision is not a usable workspace.
                    continue
            summaries.append(
                {**summary_from_manifest(manifest), "owner_id": manifest.owner.owner_id}
                if self._repositories is not None and manifest is not None
                else summary_from_manifest(manifest)
                if manifest is not None
                else self._synthetic_summary(workspace_id)
            )
        summaries.sort(key=lambda summary: str(summary["id"]))
        return tuple(summaries)

    def get(
        self, workspace_id: str, *, include_deleted: bool = False
    ) -> dict[str, object] | None:
        if self.is_deleted(workspace_id):
            if not include_deleted:
                return None
            content = (
                self._container(workspace_id).download_blob(DELETION_PATH).readall()
            )
            return json.loads(content.decode("utf-8"))["workspace"]
        if self._repositories is not None:
            try:
                manifest = self._repositories.open(workspace_id).manifest
            except WorkspaceNotFoundError:
                return None
            return {
                **summary_from_manifest(manifest),
                "owner_id": manifest.owner.owner_id,
            }
        container = self._container(workspace_id)
        manifest = self._read_manifest(container)
        if manifest is not None:
            return {
                **summary_from_manifest(manifest),
                "owner_id": manifest.owner.owner_id,
            }
        if not self._container_exists(container):
            return None
        return self._synthetic_summary(workspace_id)

    def add(self, summary: dict[str, object]) -> dict[str, object]:
        """Provision the workspace container, manifest, and return its summary."""
        workspace_id = str(summary["id"])
        if self.is_deleted(workspace_id):
            raise ValueError("workspace has been deleted")
        container = self._container(workspace_id)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
        manifest = azure_manifest_for_summary(self._template, summary)
        if self._repositories is not None:
            existing = self._read_manifest(container)
            if existing is None:
                try:
                    content = container.download_blob(PROVISION_PATH).readall()
                    existing = parse_workspace_manifest(content.decode("utf-8"))
                except ResourceNotFoundError:
                    pass
            if existing is None:
                try:
                    existing = self._repositories.open(workspace_id).manifest
                except WorkspaceNotFoundError:
                    pass
            if existing is not None:
                if (
                    existing.workspace_id != manifest.workspace_id
                    or existing.owner != manifest.owner
                    or existing.charter.title != manifest.charter.title
                    or existing.charter.objective != manifest.charter.objective
                ):
                    raise ValueError(
                        "workspace provision conflicts with existing identity"
                    )
                manifest = existing
            container.upload_blob(
                PROVISION_PATH, dump_workspace_manifest(manifest), overwrite=False
            )
            manifest = self._repositories.create(manifest).manifest
        container.upload_blob(
            MANIFEST_PATH, dump_workspace_manifest(manifest), overwrite=True
        )
        if self._repositories is not None:
            # Persist a secret-free mapping only after the scaffold reached Git.
            container.upload_blob(
                REPOSITORY_PATH,
                json.dumps(
                    {
                        "workspaceId": manifest.workspace_id,
                        "ownerId": manifest.owner.owner_id,
                        "repositoryId": manifest.repository_identity.repository_id,
                        "gitRepositoryUri": self._repositories.repository_uri(
                            workspace_id
                        ),
                    },
                    sort_keys=True,
                ),
                overwrite=True,
            )
        return {**summary_from_manifest(manifest), "owner_id": manifest.owner.owner_id}

    def mark_deleted(self, workspace_id: str, deleted_at: str) -> None:
        """Retain a create-only marker covering Blob, Git and runtime storage."""
        if self.is_deleted(workspace_id):
            return
        summary = self.get(workspace_id)
        if summary is None:
            raise KeyError(workspace_id)
        try:
            self._container(workspace_id).upload_blob(
                DELETION_PATH,
                json.dumps(
                    {
                        "workspace_id": workspace_id,
                        "status": "deletion_pending",
                        "scope": "all_workspace_data",
                        "deleted_at": deleted_at,
                        "workspace": summary,
                    }
                ),
                overwrite=False,
            )
        except ResourceExistsError:
            pass

    def is_deleted(self, workspace_id: str) -> bool:
        try:
            self._container(workspace_id).download_blob(DELETION_PATH).readall()
        except ResourceNotFoundError:
            return False
        return True

    def deleted_workspace_ids(self) -> tuple[str, ...]:
        return tuple(
            properties.name[len(self._prefix) :]
            for properties in self._blob_service.list_containers(
                name_starts_with=self._prefix
            )
            if self.is_deleted(properties.name[len(self._prefix) :])
        )

    def add_activity(self, item: dict[str, object]) -> dict[str, object]:
        workspace_id = str(item["workspace_id"])
        container = self._container(workspace_id)
        activity = dict(self._read_activity(container))
        current = activity.get(workspace_id, [])
        current.insert(0, dict(item))
        activity[workspace_id] = current
        container.upload_blob(
            ACTIVITY_PATH,
            json.dumps(
                activity, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            ),
            overwrite=True,
        )
        return dict(item)

    def activity(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        container = self._container(workspace_id)
        return tuple(
            dict(entry)
            for entry in self._read_activity(container).get(workspace_id, ())
        )

    def _read_activity(self, container: ContainerClient) -> ActivityDocument:
        try:
            content = container.download_blob(ACTIVITY_PATH).readall()
        except ResourceNotFoundError:
            return {}
        try:
            document = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(document, dict):
            return {}
        return {
            str(key): [dict(entry) for entry in value if isinstance(entry, dict)]
            for key, value in document.items()
            if isinstance(value, list)
        }

    def _container_exists(self, container: ContainerClient) -> bool:
        try:
            container.get_container_properties()
        except ResourceNotFoundError:
            return False
        return True


__all__ = [
    "ACTIVITY_PATH",
    "AzureWorkspaceCatalogStore",
    "DEFAULT_CONTAINER_PREFIX",
    "MANIFEST_PATH",
    "azure_manifest_for_summary",
    "workspace_container_name",
]
