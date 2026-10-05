"""Azure Blob-backed durable persistence for workspace chat records.

Workspace content is isolated per workspace, so chat records are written into
each workspace's own provisioned blob container (``thesis-ws-<workspace-id>``
by default) under ``repository/chats/``.  That keeps the same isolation
guarantee the workspace registry and event stores rely on and never shares a
data-plane namespace across workspaces.

New conversation payloads are root-indexed, with point-lookup references at the
legacy paths. Root threads stay at their original paths so tombstoning is one
durable overwrite. Scoped enumeration still scans legacy paths for unindexed
payloads; Blob Storage cannot filter their ownership server-side.

The adapter is deliberately transport-thin: it accepts an injected
:class:`~azure.storage.blob.BlobServiceClient` (composed with a managed
identity at the application boundary) and serializes records with the exact
payload format used by the filesystem store, so the two stores are wire- and
behaviour-compatible.  No account name, credential, or connection string is
read or hard-coded here.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Mapping
from datetime import datetime
from typing import TypeVar, cast
from urllib.parse import quote

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient

from thesis.chat.contracts import (
    ChatContractError,
    ChatState,
    ChatThread,
    ConversationRecord,
    replace_record,
)
from thesis.chat.persistence import (
    Record,
    RecordType,
    conversation_lock,
    ensure_ownership_unchanged,
    ensure_supported_record_type,
    record_from_payload,
    record_identity,
    record_payload,
    records,
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

    def _conversation_prefix(self, root: str) -> str:
        return f"{self._path_prefix}/conversations/{quote(root, safe='')}/"

    def _lock_namespace(self, workspace_id: str) -> str:
        return (
            f"azure-blob:{self._blob_service.url.rstrip('/')}/"
            f"{workspace_container_name(workspace_id, self._container_prefix)}/"
            f"{self._path_prefix}"
        )

    def save(self, record: RecordT) -> RecordT:
        if isinstance(record, ConversationRecord):
            with conversation_lock(
                self._lock_namespace(record.workspace_id),
                record.workspace_id,
                record.chat_id,
            ):
                root = self.get(ChatThread, record.workspace_id, record.chat_id)
                if root is not None and root.state is ChatState.DELETED:
                    raise ChatContractError("conversation has been deleted")
                ensure_ownership_unchanged(
                    self.get(
                        type(record), record.workspace_id, record_identity(record)
                    ),
                    record,
                )
                return self._save(record)
        return self._save(record)

    def _save(self, record: RecordT) -> RecordT:
        record_type = cast(RecordType, type(record))
        ensure_supported_record_type(record_type)
        container = self._ensure_container(record.workspace_id)
        document = json.dumps(
            {"record": record_payload(record)},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        name = self._blob_name(record_type, record_identity(record))
        # Keep the deletion fence authoritative even if another upload fails.
        if isinstance(record, ConversationRecord) and not isinstance(
            record, ChatThread
        ):
            indexed_name = (
                f"{self._conversation_prefix(record.chat_id)}"
                f"{record_type.__name__}/{quote(record_identity(record), safe='')}.json"
            )
            container.upload_blob(indexed_name, document, overwrite=True)
            # Publish point lookup only after the indexed payload is durable.
            container.upload_blob(
                name, json.dumps({"blob": indexed_name}), overwrite=True
            )
        else:
            container.upload_blob(name, document, overwrite=True)
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
            document = json.loads(content.decode("utf-8"))
            if isinstance(document, Mapping) and "blob" in document:
                content = container.download_blob(
                    self._reference_name(document)
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
        selected = self._list_records(
            typed, workspace_id, f"{self._path_prefix}/{typed.__name__}/"
        )
        if issubclass(typed, ConversationRecord):
            selected.extend(
                self._list_records(
                    typed, workspace_id, f"{self._path_prefix}/conversations/"
                )
            )
        unique = {record_identity(record): record for record in selected}
        ordered = sorted(
            unique.values(),
            key=lambda record: (record.created_at, record_identity(record)),
        )
        return cast(tuple[RecordT, ...], tuple(ordered))

    def _list_records(
        self, record_type: RecordType, workspace_id: str, prefix: str
    ) -> builtins.list[Record]:
        container = self._container(workspace_id)
        try:
            names = [
                blob.name
                for blob in container.list_blobs(name_starts_with=prefix)
                if f"/{record_type.__name__}/" in blob.name
            ]
        except ResourceNotFoundError:
            return []
        selected = []
        for name in names:
            try:
                content = container.download_blob(name).readall()
            except ResourceNotFoundError:
                continue
            document = json.loads(content.decode("utf-8"))
            if isinstance(document, Mapping) and "blob" in document:
                continue
            record = self._decode(record_type, content)
            if record.workspace_id == workspace_id and not (
                isinstance(record, ChatThread) and record.state is ChatState.DELETED
            ):
                selected.append(record)
        return selected

    def list_conversation(
        self,
        record_type: type[RecordT],
        workspace_id: str,
        root_conversation_id: str,
        *,
        conversation_id: str | None = None,
    ) -> tuple[RecordT, ...]:
        typed = cast(RecordType, record_type)
        ensure_supported_record_type(typed)
        if not issubclass(typed, ConversationRecord):
            raise TypeError("record type is not conversation-owned")
        selected = self._list_records(
            typed, workspace_id, f"{self._path_prefix}/{typed.__name__}/"
        )
        # Pre-index records cannot be filtered by ownership on the blob server.
        selected.extend(
            self._list_records(
                typed,
                workspace_id,
                f"{self._conversation_prefix(root_conversation_id)}{typed.__name__}/",
            )
        )
        selected = [
            record
            for record in selected
            if isinstance(record, ConversationRecord)
            and record.root_conversation_id == root_conversation_id
            and (conversation_id is None or record.conversation_id == conversation_id)
        ]
        unique = {record_identity(record): record for record in selected}
        ordered = sorted(
            unique.values(),
            key=lambda record: (record.created_at, record_identity(record)),
        )
        return cast(tuple[RecordT, ...], tuple(ordered))

    def mark_conversation_deleted(
        self, workspace_id: str, chat_id: str, deleted_at: datetime
    ) -> None:
        with conversation_lock(
            self._lock_namespace(workspace_id), workspace_id, chat_id
        ):
            root = self.get(ChatThread, workspace_id, chat_id)
            if root is not None and root.state is not ChatState.DELETED:
                self._save(
                    replace_record(root, state=ChatState.DELETED, updated_at=deleted_at)
                )

    def purge_conversation(
        self, workspace_id: str, chat_id: str, limit: int = 100
    ) -> bool:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with conversation_lock(
            self._lock_namespace(workspace_id), workspace_id, chat_id
        ):
            root = self.get(ChatThread, workspace_id, chat_id)
            if root is None or root.state is not ChatState.DELETED:
                raise ChatContractError("purge requires a deleted root conversation")
            remaining = limit
            for record_type in records():
                if record_type is ChatThread or not issubclass(
                    record_type, ConversationRecord
                ):
                    continue
                for record in self.list_conversation(
                    record_type, workspace_id, chat_id
                ):
                    if remaining == 0:
                        return False
                    self._delete(
                        record_type, workspace_id, record_identity(record), root=chat_id
                    )
                    remaining -= 1
            return True

    def list_deleted_conversations(self, workspace_id: str) -> tuple[ChatThread, ...]:
        container = self._container(workspace_id)
        try:
            names = [
                blob.name
                for blob in container.list_blobs(
                    name_starts_with=f"{self._path_prefix}/ChatThread/"
                )
            ]
        except ResourceNotFoundError:
            return ()
        result = []
        for name in names:
            root = self._decode(ChatThread, container.download_blob(name).readall())
            if isinstance(root, ChatThread) and root.state is ChatState.DELETED:
                result.append(root)
        return tuple(result)

    def delete(
        self, record_type: RecordType, workspace_id: str, record_id: str
    ) -> None:
        if record_type is ChatThread:
            with conversation_lock(
                self._lock_namespace(workspace_id), workspace_id, record_id
            ):
                root = self.get(ChatThread, workspace_id, record_id)
                if root is not None and root.state is ChatState.DELETED:
                    raise ChatContractError("a conversation tombstone must be retained")
                self._delete(record_type, workspace_id, record_id)
            return
        self._delete(record_type, workspace_id, record_id)

    def _delete(
        self,
        record_type: RecordType,
        workspace_id: str,
        record_id: str,
        *,
        root: str | None = None,
    ) -> None:
        container = self._container(workspace_id)
        if root is not None:
            try:
                container.delete_blob(
                    f"{self._conversation_prefix(root)}{record_type.__name__}/"
                    f"{quote(record_id, safe='')}.json"
                )
            except ResourceNotFoundError:
                pass
        name = self._blob_name(record_type, record_id)
        try:
            content = container.download_blob(name).readall()
            document = json.loads(content.decode("utf-8"))
            if isinstance(document, Mapping) and "blob" in document:
                try:
                    container.delete_blob(self._reference_name(document))
                except ResourceNotFoundError:
                    pass
            elif (
                issubclass(record_type, ConversationRecord)
                and record_type is not ChatThread
            ):
                record = self._decode(record_type, content)
                if isinstance(record, ConversationRecord):
                    try:
                        container.delete_blob(
                            f"{self._conversation_prefix(record.chat_id)}"
                            f"{record_type.__name__}/{quote(record_id, safe='')}.json"
                        )
                    except ResourceNotFoundError:
                        pass
            container.delete_blob(name)
        except ResourceNotFoundError:
            return

    def _reference_name(self, document: Mapping[str, object]) -> str:
        name = document["blob"]
        if not isinstance(name, str) or not name.startswith(
            f"{self._path_prefix}/conversations/"
        ):
            raise ValueError("malformed Azure chat persistence reference")
        return name

    def _decode(self, record_type: RecordType, content: bytes) -> Record:
        document = json.loads(content.decode("utf-8"))
        if not isinstance(document, Mapping) or "record" not in document:
            raise ValueError("malformed Azure chat persistence document")
        payload = document["record"]
        if not isinstance(payload, Mapping):
            raise ValueError("malformed Azure chat persistence record")
        return record_from_payload(record_type, payload)


__all__ = ["AzureBlobChatPersistence", "workspace_container_name"]
