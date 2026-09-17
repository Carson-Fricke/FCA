"""Azure Blob-backed workspace catalog (one container per workspace).

In Azure mode a workspace is not a directory: it is a dedicated blob container
named ``<prefix><workspace-id>`` holding the workspace's manifest at
``repository/.thesis/workspace.yaml``.  This store implements the same
:class:`~thesis.http.workspaces.WorkspaceStore` port as the filesystem catalog,
so the workspace routes, the overview, and the UI all see the same durable
list.  The manifest in the container is the source of truth; a container that
has not yet received a manifest is still listed from its name so a
provisioned-but-unseeded workspace is visible rather than silently missing.

The adapter is transport-thin: it accepts an injected
:class:`~azure.storage.blob.BlobServiceClient` (composed with the runtime
managed identity) and never reads an account name or credential itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient

from thesis.workspaces.catalog import summary_from_manifest
from thesis.workspaces.manifest import (
    WorkspaceManifest,
    dump_workspace_manifest,
    parse_workspace_manifest,
)

MANIFEST_PATH = "repository/.thesis/workspace.yaml"
ACTIVITY_PATH = "repository/.catalog/activity.json"
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
    ) -> None:
        if not container_prefix.strip():
            raise ValueError("container_prefix must be a non-empty string")
        self._blob_service = blob_service
        self._template = template
        self._prefix = container_prefix
        self._clock = clock if clock is not None else _utc_now
        self._containers: dict[str, ContainerClient] = {}

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
            manifest = self._read_manifest(
                self._blob_service.get_container_client(name)
            )
            summaries.append(
                summary_from_manifest(manifest)
                if manifest is not None
                else self._synthetic_summary(workspace_id)
            )
        summaries.sort(key=lambda summary: str(summary["id"]))
        return tuple(summaries)

    def get(self, workspace_id: str) -> dict[str, object] | None:
        container = self._container(workspace_id)
        manifest = self._read_manifest(container)
        if manifest is not None:
            return summary_from_manifest(manifest)
        if not self._container_exists(container):
            return None
        return self._synthetic_summary(workspace_id)

    def add(self, summary: dict[str, object]) -> dict[str, object]:
        """Provision the workspace container, manifest, and return its summary."""
        workspace_id = str(summary["id"])
        container = self._container(workspace_id)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
        manifest = azure_manifest_for_summary(self._template, summary)
        container.upload_blob(
            MANIFEST_PATH, dump_workspace_manifest(manifest), overwrite=True
        )
        return summary_from_manifest(manifest)

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
