"""Canonical chat records, admission, consumption windows and deletion."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

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
    ChatContractError,
    ChatMessage,
    ChatState,
    ChatThread,
    ConversationRecord,
    RecordNotFound,
    WorkflowExecution,
    replace_record,
)
from thesis_runtime.conversations.admission import (
    existing_admission,
    message_bookkeeping,
    message_order_key,
    position_message,
    require_admission_chat,
    require_live_chat,
)

from .store import PostgresStore, identity
from .timeline import PostgresTimeline

RecordT = TypeVar("RecordT", bound=Record)


def inbox_head_id(chat_id: str, conversation_id: str) -> str:
    return identity("ConversationInbox:", chat_id + "\0" + conversation_id)


class PostgresChatPersistence:
    def __init__(self, store: PostgresStore, timeline: PostgresTimeline) -> None:
        self.store, self.timeline = store, timeline

    @staticmethod
    def _document(record: Record) -> dict[str, Any]:
        document: dict[str, Any] = {
            "id": type(record).__name__ + ":" + record_identity(record),
            "workspaceId": record.workspace_id,
            "recordType": type(record).__name__,
            "createdAt": record.created_at.isoformat(),
            "payload": record_payload(record),
        }
        if isinstance(record, ConversationRecord):
            document.update(
                chatId=record.chat_id,
                conversationId=record.conversation_id,
                rootConversationId=record.root_conversation_id,
                parentConversationId=record.parent_conversation_id,
            )
        if isinstance(record, ChatMessage):
            anchor = (record.data or {}).get("subAgentId")
            document.update(
                runTurn=(record.data or {}).get("runTurn") is True,
                messageId=record.message_id,
                inputPosition=record.input_position,
                kind="message",
                threadId=anchor or "root",
                ts=record.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            )
        elif isinstance(record, WorkflowExecution):
            document.update(
                kind="execution",
                threadId="root",
                initialPayload=record.as_dict(),
                ts=record.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            )
        return document

    def admit_message(self, message: ChatMessage) -> ChatMessage:
        with self.store.transaction(
            message.workspace_id, chat_id=message.chat_id
        ) as connection:
            key = "ChatMessage:" + message.message_id
            previous = self.store.read(message.workspace_id, key, connection=connection)
            if previous is not None:
                return existing_admission(
                    cast(
                        ChatMessage,
                        record_from_payload(ChatMessage, previous["payload"]),
                    ),
                    message,
                )
            root = self.store.read(
                message.workspace_id,
                "ChatThread:" + message.chat_id,
                connection=connection,
            )
            chat = require_admission_chat(
                cast(ChatThread, record_from_payload(ChatThread, root["payload"]))
                if root
                else None
            )
            seq = self.timeline.next_sequence(
                connection, message.workspace_id, message.chat_id
            )
            admitted = position_message(message, seq, arrival_sequence=seq)
            self.timeline.publish(connection, self._document(admitted))
            self.store.put(
                connection, self._document(message_bookkeeping(chat, admitted))
            )
            if is_external_input(admitted):
                self.store.put(
                    connection,
                    {
                        "id": inbox_head_id(message.chat_id, message.conversation_id),
                        "workspaceId": message.workspace_id,
                        "chatId": message.chat_id,
                        "recordType": "ConversationInbox",
                        "position": seq,
                    },
                )
        self.timeline.commits.notify(message.workspace_id, message.chat_id)
        return admitted

    def update_chat(
        self,
        workspace_id: str,
        chat_id: str,
        update: Callable[[ChatThread], ChatThread],
    ) -> ChatThread:
        # Deletion itself passes through this boundary, so check liveness here.
        with self.store.transaction(workspace_id) as connection:
            document = self.store.read(
                workspace_id, "ChatThread:" + chat_id, connection=connection
            )
            root = require_live_chat(
                cast(ChatThread, record_from_payload(ChatThread, document["payload"]))
                if document
                else None
            )
            updated = update(root)
            ensure_ownership_unchanged(root, updated)
            if updated is not root:
                self.store.put(connection, self._document(updated))
            return updated

    def save(self, record: RecordT) -> RecordT:
        typed = cast(RecordType, type(record))
        ensure_supported_record_type(typed)
        chat_id = record.chat_id if isinstance(record, ConversationRecord) else None
        with self.store.transaction(record.workspace_id, chat_id=chat_id) as connection:
            document = self._document(record)
            previous = self.store.read(
                record.workspace_id, document["id"], connection=connection
            )
            if previous:
                ensure_ownership_unchanged(
                    record_from_payload(typed, previous["payload"]), record
                )
                if isinstance(record, ChatThread):
                    raise ChatContractError("existing chats require update_chat")
                for name in (
                    "seq",
                    "initialPayload",
                    "kind",
                    "threadId",
                    "executionId",
                ):
                    if name in previous:
                        document[name] = previous[name]
            if previous is None and isinstance(
                record, (ChatMessage, WorkflowExecution)
            ):
                self.timeline.publish(connection, document)
            else:
                self.store.put(
                    connection, document, create=isinstance(record, ChatThread)
                )
        if chat_id:
            self.timeline.commits.notify(record.workspace_id, chat_id)
        return record

    def get(
        self, record_type: type[RecordT], workspace_id: str, record_id: str
    ) -> RecordT | None:
        typed = cast(RecordType, record_type)
        ensure_supported_record_type(typed)
        document = self.store.read(workspace_id, typed.__name__ + ":" + record_id)
        if document is None:
            return None
        record = record_from_payload(typed, document["payload"])
        if record.workspace_id != workspace_id or record_identity(record) != record_id:
            raise ChatContractError("record identity does not match its storage scope")
        return cast(RecordT, record)

    def list(
        self, record_type: type[RecordT], workspace_id: str
    ) -> tuple[RecordT, ...]:
        return self._list(record_type, workspace_id)

    def list_conversation(
        self,
        record_type: type[RecordT],
        workspace_id: str,
        root_conversation_id: str,
        *,
        conversation_id: str | None = None,
    ) -> tuple[RecordT, ...]:
        if not issubclass(record_type, ConversationRecord):
            raise TypeError("record type is not conversation-owned")
        return self._list(
            record_type, workspace_id, root_conversation_id, conversation_id
        )

    def _list(
        self,
        record_type: type[RecordT],
        workspace_id: str,
        root: str | None = None,
        conversation: str | None = None,
    ) -> tuple[RecordT, ...]:
        typed = cast(RecordType, record_type)
        ensure_supported_record_type(typed)
        where, args = (
            "workspace_id=%s AND record_type=%s",
            [workspace_id, typed.__name__],
        )
        if typed is ChatThread:
            where += " AND state != 'deleted'"
        if root is not None:
            where += " AND root_conversation_id=%s"
            args.append(root)
        if conversation is not None:
            where += " AND conversation_id=%s"
            args.append(conversation)
        rows = self.store.query(where, args, order="created_at,id")
        return tuple(
            cast(RecordT, record_from_payload(typed, row["payload"])) for row in rows
        )

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
        if after_position is not None:
            head = self.store.read(
                workspace_id, inbox_head_id(chat_id, conversation_id)
            )
            if head is None or int(head["position"]) <= after_position:
                return ()
        where = "workspace_id=%s AND root_conversation_id=%s AND conversation_id=%s AND record_type='ChatMessage'"
        args: list[object] = [workspace_id, chat_id, conversation_id]
        rows = (
            self.store.query(where, args, order="seq DESC", limit=limit)
            if after_position is None
            else []
        )
        pending = self.store.query(
            where + " AND input_position>%s",
            [*args, consumed_position if after_position is None else after_position],
            order="seq",
        )
        unique = {row["id"]: row for row in [*rows, *pending]}
        return tuple(
            sorted(
                (
                    cast(ChatMessage, record_from_payload(ChatMessage, row["payload"]))
                    for row in unique.values()
                ),
                key=message_order_key,
            )
        )

    def require_active_conversation(self, workspace_id: str, chat_id: str) -> None:
        require_live_chat(self.get(ChatThread, workspace_id, chat_id))

    def scope_for_execution(self, execution_id: str) -> tuple[str, str]:
        rows = self.store.query(
            "record_type='WorkflowExecution' AND id=%s",
            ["WorkflowExecution:" + execution_id],
            columns="workspace_id,chat_id",
            limit=2,
        )
        if len(rows) != 1:
            raise KeyError("unknown execution " + execution_id)
        return rows[0]

    def mark_conversation_deleted(
        self, workspace_id: str, chat_id: str, deleted_at: datetime
    ) -> None:
        try:
            self.update_chat(
                workspace_id,
                chat_id,
                lambda root: replace_record(
                    root, state=ChatState.DELETED, updated_at=deleted_at
                ),
            )
        except RecordNotFound:
            return

    def list_deleted_conversations(self, workspace_id: str) -> tuple[ChatThread, ...]:
        return tuple(
            cast(ChatThread, record_from_payload(ChatThread, row["payload"]))
            for row in self.store.query(
                "workspace_id=%s AND record_type='ChatThread' AND state='deleted'",
                [workspace_id],
            )
        )

    def purge_conversation(
        self, workspace_id: str, chat_id: str, limit: int = 100
    ) -> bool:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 100)
        with self.store.transaction(workspace_id) as connection:
            root = self.store.read(
                workspace_id, "ChatThread:" + chat_id, connection=connection
            )
            if root is None or root["payload"]["state"] != "deleted":
                raise ChatContractError("purge requires a deleted root conversation")
            rows = self.store.query(
                "workspace_id=%s AND chat_id=%s AND id<>%s",
                [workspace_id, chat_id, root["id"]],
                columns="id",
                limit=limit + 1,
                connection=connection,
            )
            for row in rows[:limit]:
                self.store.delete(connection, workspace_id, row[0])
            return len(rows) <= limit

    def delete(
        self, record_type: RecordType, workspace_id: str, record_id: str
    ) -> None:
        with self.store.transaction(workspace_id) as connection:
            key = record_type.__name__ + ":" + record_id
            document = self.store.read(workspace_id, key, connection=connection)
            if (
                record_type is ChatThread
                and document
                and document["payload"]["state"] == "deleted"
            ):
                raise ChatContractError("a conversation tombstone must be retained")
            self.store.delete(connection, workspace_id, key)
