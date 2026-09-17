"""Azure Blob-backed durable persistence for workspace chat records.

Workspace content is isolated per workspace, so chat records are written into
each workspace's own provisioned blob container (``thesis-ws-<workspace-id>``
by default) under ``repository/chats/``.  That keeps the same isolation
guarantee the workspace registry and event stores rely on and never shares a
data-plane namespace across workspaces.

The adapter is deliberately transport-thin: it accepts an injected
:class:`~azure.storage.blob.BlobServiceClient` (composed with a managed
identity at the application boundary) and serializes records with the exact
payload format used by the filesystem store, so the two stores are wire- and
behaviour-compatible.  No account name, credential, or connection string is
read or hard-coded here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypeVar, cast
from urllib.parse import quote

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient

from thesis.chat.persistence import (
    Record,
    RecordType,
    ensure_supported_record_type,
    record_from_payload,
    record_identity,
    record_payload,
)
from thesis.workspaces.azure_catalog import workspace_container_name

RecordT = TypeVar(
    "RecordT",
    bound=Record,
)

_DEFAULT_CONTAINER_PREFIX = "thesis-ws-"
_DEFAULT_PATH_PREFIX = "repository/chats"


class AzureBlobChatPersistence:
    """Durable chat-record persistence in the workspace's blob container.

    Satisfies the structural
    :class:`~thesis.chat.service.ChatPersistence` protocol, so it drops into
    :class:`~thesis.chat.service.ChatService` in place of the in-memory or
    filesystem adapters.
    """

    def __init__(
        self,
        blob_service: BlobServiceClient,
        *,
        container_prefix: str = _DEFAULT_CONTAINER_PREFIX,
        path_prefix: str = _DEFAULT_PATH_PREFIX,
    ) -> None:
        if not container_prefix.strip():
            raise ValueError("container_prefix must be a non-empty string")
        self._blob_service = blob_service
        self._container_prefix = container_prefix
        self._path_prefix = path_prefix.strip("/")
        self._containers: dict[str, ContainerClient] = {}
        self._ready: set[str] = set()

    @property
    def container_prefix(self) -> str:
        """Return the workspace container prefix this store writes under."""
        return self._container_prefix

    def _container(self, workspace_id: str) -> ContainerClient:
        name = workspace_container_name(workspace_id, self._container_prefix)
        client = self._containers.get(name)
        if client is None:
            client = self._blob_service.get_container_client(name)
            self._containers[name] = client
        return client

    def _ensure_container(self, workspace_id: str) -> ContainerClient:
        container = self._container(workspace_id)
        name = workspace_container_name(workspace_id, self._container_prefix)
        if name not in self._ready:
            try:
                container.create_container()
            except ResourceExistsError:
                pass
            self._ready.add(name)
        return container

    def _blob_name(self, record_type: RecordType, record_id: str) -> str:
        encoded = quote(record_id, safe="")
        return f"{self._path_prefix}/{record_type.__name__}/{encoded}.json"

    def save(self, record: RecordT) -> RecordT:
        record_type = cast(RecordType, type(record))
        ensure_supported_record_type(record_type)
        container = self._ensure_container(record.workspace_id)
        document = json.dumps(
            {"record": record_payload(record)},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        container.upload_blob(
            self._blob_name(record_type, record_identity(record)),
            document,
            overwrite=True,
        )
        return record

    def get(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT | None:
        typed = cast(RecordType, record_type)
        ensure_supported_record_type(typed)
        container = self._container(workspace_id)
        try:
            content = container.download_blob(
                self._blob_name(typed, record_id)
            ).readall()
        except ResourceNotFoundError:
            return None
        record = self._decode(typed, content)
        if record.workspace_id != workspace_id or record_identity(record) != record_id:
            return None
        return cast(RecordT, record)

    def list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]:
        typed = cast(RecordType, record_type)
        ensure_supported_record_type(typed)
        container = self._container(workspace_id)
        prefix = f"{self._path_prefix}/{typed.__name__}/"
        try:
            names = [
                blob.name for blob in container.list_blobs(name_starts_with=prefix)
            ]
        except ResourceNotFoundError:
            return ()
        records = [
            self._decode(typed, container.download_blob(name).readall())
            for name in names
        ]
        selected = [record for record in records if record.workspace_id == workspace_id]
        selected.sort(key=lambda record: (record.created_at, record_identity(record)))
        return cast(tuple[RecordT, ...], tuple(selected))

    def delete(
        self, record_type: RecordType, workspace_id: str, record_id: str
    ) -> None:
        container = self._container(workspace_id)
        try:
            container.delete_blob(self._blob_name(record_type, record_id))
        except ResourceNotFoundError:
            return

    def _decode(self, record_type: RecordType, content: bytes) -> Record:
        document = json.loads(content.decode("utf-8"))
        if not isinstance(document, Mapping) or "record" not in document:
            raise ValueError("malformed Azure chat persistence document")
        payload = document["record"]
        if not isinstance(payload, Mapping):
            raise ValueError("malformed Azure chat persistence record")
        return record_from_payload(record_type, payload)


__all__ = ["AzureBlobChatPersistence", "workspace_container_name"]
