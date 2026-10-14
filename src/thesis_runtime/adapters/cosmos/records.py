"""Cosmos DB-backed durable persistence for workspace chat records.

Implements the ``ChatPersistence`` port so chats, messages, executions,
attachments and tool jobs persist in the one ``catalog``
container, partitioned by ``workspaceId``. Hydrated record payloads match the
other stores. Oversized JSON is retained verbatim in immutable chunk trees;
payloads carry bounded manifests.

Messages and execution snapshots are also the conversation timeline itself:
they receive a chat-wide ``seq`` from the same transactional batch that creates
them (``CosmosTimeline``). There is no second container, no projection outbox,
and no reconciliation scan. Indexed top-level ownership fields (``chatId``,
``conversationId``, ``rootConversationId``) scope every conversation query.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosBatchOperationError,
    CosmosHttpResponseError,
    CosmosResourceNotFoundError,
)

from thesis_runtime.adapters.cosmos.chunks import (
    ChunkedPayloadStore,
    ensure_bounded_item,
)
from thesis_runtime.adapters.cosmos.ownership import fenced_batch
from thesis_runtime.adapters.cosmos.timeline import (
    KIND_EXECUTION,
    KIND_MESSAGE,
    ROOT_THREAD,
    CosmosTimeline,
)
from thesis_runtime.contracts.activity import is_external_input
from thesis_runtime.contracts.codec import (
    Record,
    RecordType,
    ensure_ownership_unchanged,
    ensure_supported_record_type,
    record_from_payload,
    record_identity,
    record_payload,
)
from thesis_runtime.contracts.records import (
    AgentInvocation,
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    ConversationRecord,
    RecordNotFound,
    WorkflowExecution,
)
from thesis_runtime.conversations.admission import (
    existing_admission,
    message_bookkeeping,
    message_order_key,
    position_message,
    require_admission_chat,
    require_live_chat,
)
from thesis_runtime.storage.locks import conversation_lock


def inbox_head_id(chat_id: str, conversation_id: str) -> str:
    import hashlib

    return (
        "ConversationInbox:"
        + hashlib.sha256(f"{chat_id}\0{conversation_id}".encode()).hexdigest()
    )


RecordT = TypeVar("RecordT", bound=Record)
MAX_REPLACE_ATTEMPTS = 32


def _timestamp(value: datetime) -> str:
    """Return the timeline's canonical UTC timestamp text."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CosmosChatPersistence:
    """Durable chat-record persistence in the workspace catalog container."""

    def __init__(self, container: ContainerProxy, *, timeline: CosmosTimeline) -> None:
        self._container = container
        self._timeline = timeline
        self._chunks = ChunkedPayloadStore(container)

    @staticmethod
    def _item_id(record_type: RecordType, record_id: str) -> str:
        return f"{record_type.__name__}:{record_id}"

    def _read_document(self, workspace_id: str, item_id: str) -> dict[str, Any] | None:
        try:
            return dict(
                self._container.read_item(item=item_id, partition_key=workspace_id)
            )
        except CosmosResourceNotFoundError:
            return None

    def _decode(self, record_type: RecordType, document: dict[str, Any]) -> Record:
        return record_from_payload(
            record_type,
            self._load_payload(document["payload"], cast(str, document["workspaceId"])),
        )

    def admit_message(self, message: ChatMessage) -> ChatMessage:
        """Commit input cursor, canonical body, chat and runnable work atomically."""
        prepared_record: ChatMessage | None = None

        def prepare(seq: int) -> tuple[dict[str, Any], list[Any]]:
            nonlocal prepared_record
            root_id = self._item_id(ChatThread, message.chat_id)
            root = self._read_document(message.workspace_id, root_id)
            chat = require_admission_chat(
                cast(ChatThread, self._decode(ChatThread, root)) if root else None
            )
            assert root is not None
            # Reuse the existing chat-wide timeline CAS as the monotonic input
            # cursor. Gaps are valid: output/event entries also receive seqs.
            admitted = position_message(message, seq, arrival_sequence=seq)
            prepared_record = admitted
            document = self._document(admitted)
            updated_root = self._document(message_bookkeeping(chat, admitted))
            operations: list[Any] = [
                ("replace", (root_id, updated_root), {"if_match_etag": root["_etag"]}),
            ]
            if is_external_input(admitted):
                operations.append(
                    (
                        "upsert",
                        (
                            {
                                "id": inbox_head_id(
                                    message.chat_id,
                                    message.conversation_id or message.chat_id,
                                ),
                                "workspaceId": message.workspace_id,
                                "chatId": message.chat_id,
                                "recordType": "ConversationInbox",
                                "position": seq,
                            },
                        ),
                    )
                )
            return document, operations

        committed = self._timeline.create_with_sequence(
            {},
            workspace_id=message.workspace_id,
            chat_id=message.chat_id,
            prepare=prepare,
        )
        durable = cast(ChatMessage, self._decode(ChatMessage, dict(committed)))
        # A conflicting create may have joined a concurrent writer. Validate the
        # durable identity instead of returning this caller's proposed snapshot.
        if durable != prepared_record:
            return existing_admission(durable, message)
        return durable

    def update_chat(
        self,
        workspace_id: str,
        chat_id: str,
        update: Callable[[ChatThread], ChatThread],
    ) -> ChatThread:
        """Merge against the root ETag shared by admission and lifecycle writes."""
        item_id = self._item_id(ChatThread, chat_id)
        for attempt in range(MAX_REPLACE_ATTEMPTS):
            document = self._read_document(workspace_id, item_id)
            root = require_live_chat(
                cast(ChatThread, self._decode(ChatThread, document))
                if document
                else None
            )
            assert document is not None
            updated = update(root)
            if updated is root:
                return root
            ensure_ownership_unchanged(root, updated)
            proposed = self._document(updated)
            ensure_bounded_item(proposed)
            try:
                fenced_batch(
                    self._container,
                    workspace_id,
                    [
                        (
                            "replace",
                            (item_id, proposed),
                            {"if_match_etag": document["_etag"]},
                        )
                    ],
                )
            except (CosmosBatchOperationError, CosmosHttpResponseError) as error:
                if error.status_code != 412 or attempt == MAX_REPLACE_ATTEMPTS - 1:
                    raise
            else:
                return updated
        raise AssertionError("unreachable chat update retry exhaustion")

    def read_message_window(
        self,
        workspace_id: str,
        chat_id: str,
        conversation_id: str,
        *,
        after_position: int | None,
        consumed_position: int,
        limit: int,
    ) -> tuple[ChatMessage, ...]:
        """Indexed bounded history; unchanged input heads need only a point read."""
        if after_position is not None:
            head = self._read_document(
                workspace_id, inbox_head_id(chat_id, conversation_id)
            )
            if head is None or int(head["position"]) <= after_position:
                return ()
        parameters: list[dict[str, object]] = [
            {"name": "@ws", "value": workspace_id},
            {"name": "@type", "value": "ChatMessage"},
            {"name": "@root", "value": chat_id},
            {"name": "@conversation", "value": conversation_id},
        ]
        scope = "FROM c WHERE c.workspaceId = @ws AND c.recordType = @type AND c.rootConversationId = @root AND c.conversationId = @conversation "
        documents: dict[str, dict[str, Any]] = {}
        if after_position is None:
            for row in self._container.query_items(
                query="SELECT TOP @limit * " + scope + "ORDER BY c.seq DESC",
                parameters=[*parameters, {"name": "@limit", "value": limit}],
                partition_key=workspace_id,
                max_item_count=limit,
            ):
                documents[row["id"]] = row
        for row in self._container.query_items(
            query="SELECT * "
            + scope
            + "AND c.inputPosition > @position ORDER BY c.seq ASC",
            parameters=[
                *parameters,
                {
                    "name": "@position",
                    "value": consumed_position
                    if after_position is None
                    else after_position,
                },
            ],
            partition_key=workspace_id,
        ):
            documents[row["id"]] = row
        messages = [
            cast(ChatMessage, self._decode(ChatMessage, row))
            for row in documents.values()
        ]
        return tuple(
            sorted(
                messages,
                key=message_order_key,
            )
        )

    def save(self, record: RecordT) -> RecordT:
        if isinstance(record, ConversationRecord):
            with conversation_lock("cosmos", record.workspace_id, record.chat_id):
                root = self.get(ChatThread, record.workspace_id, record.chat_id)
                if root is not None and root.state is ChatState.DELETED:
                    raise ChatContractError("conversation has been deleted")
                return self._save(record)
        return self._save(record)

    def _document(self, record: Record) -> dict[str, Any]:
        payload = record_payload(record)
        if isinstance(record, AgentInvocation) and record.provider_request is not None:
            payload["provider_request"] = self._chunks.dump(
                cast(dict[str, object], payload["provider_request"]),
                record.workspace_id,
                force=True,
            )
        document: dict[str, Any] = {
            "id": self._item_id(type(record), record_identity(record)),
            "workspaceId": record.workspace_id,
            "recordType": type(record).__name__,
            "createdAt": record.created_at.isoformat(),
            "payload": self._chunks.dump(payload, record.workspace_id),
        }
        if isinstance(record, ConversationRecord):
            document.update(
                chatId=record.chat_id,
                conversationId=record.conversation_id,
                rootConversationId=record.root_conversation_id,
                parentConversationId=record.parent_conversation_id,
            )
        if isinstance(record, ChatMessage):
            document["runTurn"] = (record.data or {}).get("runTurn") is True
            document["messageId"] = record.message_id
            document["inputPosition"] = record.input_position
            anchor = (record.data or {}).get("subAgentId")
            document["kind"] = KIND_MESSAGE
            document["threadId"] = (
                anchor if isinstance(anchor, str) and anchor else ROOT_THREAD
            )
            document["ts"] = _timestamp(record.created_at)
        elif isinstance(record, WorkflowExecution):
            document["kind"] = KIND_EXECUTION
            document["threadId"] = ROOT_THREAD
            document["ts"] = _timestamp(record.created_at)
            document["initialPayload"] = self._chunks.dump(
                record.as_dict(), record.workspace_id
            )
        return document

    def _save(self, record: RecordT) -> RecordT:
        record_type = type(record)
        ensure_supported_record_type(record_type)
        document = self._document(record)
        previous = self._read_document(record.workspace_id, cast(str, document["id"]))
        if previous is not None:
            ensure_ownership_unchanged(
                self._decode(cast(RecordType, record_type), previous), record
            )
        if isinstance(record, ChatThread):
            if previous is not None:
                raise ChatContractError("existing chats require update_chat")
            fenced_batch(
                self._container, record.workspace_id, [("create", (document,))]
            )
            return record
        if isinstance(record, (ChatMessage, WorkflowExecution)):
            return cast(
                RecordT,
                self._save_timeline_record(record, document, previous),
            )
        ensure_bounded_item(document)
        fenced_batch(self._container, record.workspace_id, [("upsert", (document,))])
        return record

    def _save_timeline_record(
        self,
        record: ChatMessage | WorkflowExecution,
        document: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> ChatMessage | WorkflowExecution:
        """Create with a fresh sequence, or update an existing item in place.

        The first save is immutable evidence published to the timeline; later
        updates change only the mutable payload and keep the sequence and
        first-save snapshot.
        """
        if previous is None:
            self._timeline.create_with_sequence(
                document,
                workspace_id=record.workspace_id,
                chat_id=record.chat_id,
            )
            return record
        self._preserve_first_save(document, previous)
        for _ in range(MAX_REPLACE_ATTEMPTS):
            ensure_bounded_item(document)
            try:
                fenced_batch(
                    self._container,
                    record.workspace_id,
                    [
                        (
                            "replace",
                            (document["id"], document),
                            {"if_match_etag": previous["_etag"]},
                        )
                    ],
                )
            except CosmosAccessConditionFailedError:
                previous = self._read_document(
                    record.workspace_id, cast(str, document["id"])
                )
                if previous is None:
                    raise ChatContractError(
                        "timeline record disappeared during update"
                    ) from None
                ensure_ownership_unchanged(
                    self._decode(cast(RecordType, type(record)), previous), record
                )
                self._preserve_first_save(document, previous)
                continue
            return record
        raise AssertionError("unreachable replace retry exhaustion")

    @staticmethod
    def _preserve_first_save(
        document: dict[str, Any], previous: dict[str, Any]
    ) -> None:
        document["seq"] = previous["seq"]
        for name in ("initialPayload", "kind", "threadId", "executionId"):
            if name in previous:
                document[name] = previous[name]

    def get(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT | None:
        typed = cast(RecordType, record_type)
        ensure_supported_record_type(typed)
        document = self._read_document(workspace_id, self._item_id(typed, record_id))
        if document is None:
            return None
        record = self._decode(typed, document)
        if record.workspace_id != workspace_id or record_identity(record) != record_id:
            return None
        return cast(RecordT, record)

    def _load_payload(
        self, payload: dict[str, Any], workspace_id: str
    ) -> dict[str, Any]:
        hydrated = self._chunks.load(payload, workspace_id)
        audit = hydrated.get("provider_request")
        if isinstance(audit, dict):
            hydrated["provider_request"] = self._chunks.load(audit, workspace_id)
        return hydrated

    def list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]:
        typed = cast(RecordType, record_type)
        ensure_supported_record_type(typed)
        items = list(
            self._container.query_items(
                query=(
                    "SELECT c.payload FROM c WHERE c.workspaceId = @ws AND c.recordType = @type"
                    + (
                        " AND c.payload.state != 'deleted'"
                        if typed is ChatThread
                        else ""
                    )
                ),
                parameters=[
                    {"name": "@ws", "value": workspace_id},
                    {"name": "@type", "value": typed.__name__},
                ],
                partition_key=workspace_id,
            )
        )
        records = [
            record_from_payload(
                typed, self._load_payload(item["payload"], workspace_id)
            )
            for item in items
        ]
        records.sort(key=lambda record: (record.created_at, record_identity(record)))
        return cast(tuple[RecordT, ...], tuple(records))

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
        query = (
            "SELECT c.payload FROM c WHERE c.workspaceId = @ws AND c.recordType = @type "
            "AND c.rootConversationId = @root"
        )
        parameters: list[dict[str, Any]] = [
            {"name": "@ws", "value": workspace_id},
            {"name": "@type", "value": typed.__name__},
            {"name": "@root", "value": root_conversation_id},
        ]
        if typed is ChatThread:
            query += " AND c.payload.state != 'deleted'"
        if conversation_id is not None:
            query += " AND c.conversationId = @conversation"
            parameters.append({"name": "@conversation", "value": conversation_id})
        items = self._container.query_items(
            query=query,
            parameters=parameters,
            partition_key=workspace_id,
        )
        selected = [
            record_from_payload(
                typed, self._load_payload(item["payload"], workspace_id)
            )
            for item in items
        ]
        selected.sort(key=lambda record: (record.created_at, record_identity(record)))
        return cast(tuple[RecordT, ...], tuple(selected))

    def mark_conversation_deleted(
        self,
        workspace_id: str,
        chat_id: str,
        deleted_at: datetime,
    ) -> None:
        from thesis_runtime.contracts.records import replace_record

        try:
            self.update_chat(
                workspace_id,
                chat_id,
                lambda root: replace_record(
                    root, state=ChatState.DELETED, updated_at=deleted_at
                ),
            )
        except RecordNotFound:
            # An absent root or retained tombstone is never recreated.
            return

    def list_deleted_conversations(self, workspace_id: str) -> tuple[ChatThread, ...]:
        items = self._container.query_items(
            query="SELECT c.payload FROM c WHERE c.workspaceId = @ws AND c.recordType = 'ChatThread' AND c.payload.state = 'deleted'",
            parameters=[{"name": "@ws", "value": workspace_id}],
            partition_key=workspace_id,
        )
        return tuple(
            cast(
                ChatThread,
                record_from_payload(
                    ChatThread, self._chunks.load(item["payload"], workspace_id)
                ),
            )
            for item in items
        )

    def require_active_conversation(self, workspace_id: str, chat_id: str) -> None:
        root = self.get(ChatThread, workspace_id, chat_id)
        if root is None or root.state is ChatState.DELETED:
            raise ChatContractError("conversation has been deleted")

    def scope_for_execution(self, execution_id: str) -> tuple[str, str]:
        """Resolve ``(workspace_id, chat_id)`` for one workflow execution.

        The execution's chat partitions the conversation timeline, and the
        workspace partitions the catalog; callers query events under both.
        """
        items = list(
            self._container.query_items(
                query=(
                    "SELECT c.payload, c.workspaceId FROM c "
                    "WHERE c.recordType = @type AND c.id = @id"
                ),
                parameters=[
                    {"name": "@type", "value": "WorkflowExecution"},
                    {"name": "@id", "value": f"WorkflowExecution:{execution_id}"},
                ],
                enable_cross_partition_query=True,
            )
        )
        if not items:
            raise KeyError(f"unknown execution {execution_id!r}")
        workspace_id = cast(str, items[0]["workspaceId"])
        payload = self._chunks.load(items[0]["payload"], workspace_id)
        chat_id = payload.get("chat_id")
        if not isinstance(chat_id, str):
            raise KeyError(f"unknown execution {execution_id!r}")
        return workspace_id, chat_id

    def purge_conversation(
        self,
        workspace_id: str,
        chat_id: str,
        limit: int = 100,
    ) -> bool:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 100)
        with conversation_lock("cosmos", workspace_id, chat_id):
            root = self.get(ChatThread, workspace_id, chat_id)
            if root is None or root.state is not ChatState.DELETED:
                raise ChatContractError("purge requires a deleted root conversation")
            items = list(
                self._container.query_items(
                    query=(
                        "SELECT TOP @count c.id FROM c WHERE c.workspaceId = @ws "
                        "AND c.chatId = @chat AND c.id != @thread"
                    ),
                    parameters=[
                        {"name": "@ws", "value": workspace_id},
                        {"name": "@chat", "value": chat_id},
                        {
                            "name": "@thread",
                            "value": self._item_id(ChatThread, chat_id),
                        },
                        {"name": "@count", "value": limit + 1},
                    ],
                    partition_key=workspace_id,
                )
            )
            if items:
                self._container.execute_item_batch(
                    batch_operations=[
                        ("delete", (item["id"],)) for item in items[:limit]
                    ],
                    partition_key=workspace_id,
                )
            return len(items) <= limit

    def delete(
        self, record_type: RecordType, workspace_id: str, record_id: str
    ) -> None:
        if record_type is ChatThread:
            with conversation_lock("cosmos", workspace_id, record_id):
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
    ) -> None:
        try:
            self._container.delete_item(
                item=self._item_id(record_type, record_id), partition_key=workspace_id
            )
        except CosmosResourceNotFoundError:
            return


__all__ = ["CosmosChatPersistence"]
